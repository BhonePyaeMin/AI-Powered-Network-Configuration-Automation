"""
Topology editing for the dashboard chatbot ("model update").

Instead of asking an LLM to rewrite a whole GNS3 project file (huge, easily truncated), an
instruction is turned into a few small operations - add / remove / connect - that are applied
here deterministically. Simple commands ("Add 1 more PC", "Add a switch", "Remove PC4") are
parsed with rules and need no API key; free-form requests fall back to Gemini, which only has to
return a short list of operations.

sync_new_devices() then creates the devices that exist only in the website model inside the
real GNS3 project (nodes + links) so they can be configured like the rest of the lab.
"""
import copy
import ipaddress
import re
import socket
import time
import uuid

import gns3_service as g

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class OpError(Exception):
    """A user-facing problem with an edit request."""


KIND_PREFIX = {"pc": "PC", "router": "R", "switch": "Switch"}
KIND_NODE_TYPE = {"pc": "vpcs", "router": "dynamips", "switch": "ethernet_switch"}
KIND_WORDS = {
    "pc": r"(?:pcs?|vpcs?|computers?|hosts?)",
    "router": r"routers?",
    "switch": r"(?:switch(?:es)?|sw)",
}
NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "another": 1, "two": 2, "three": 3, "four": 4, "five": 5}


# ───────────────────────────── ports ──────────────────────────────────────────

def _kind(node):
    return g._node_kind(node.get("node_type"))


def _ports_for(node):
    t = (node.get("node_type") or "").lower()
    if t == "vpcs":
        return [(0, 0)]
    if "switch" in t or t == "ethernet_hub":
        return [(0, i) for i in range(8)]
    return [(0, 0), (1, 0), (1, 1)]  # Cisco 7200 as used in this lab: f0/0, f1/0, f1/1


def _port_label(node, adapter, port):
    t = (node.get("node_type") or "").lower()
    if t == "vpcs":
        return "eth0"
    if "switch" in t or t == "ethernet_hub":
        return f"e{port}"
    return f"f{adapter}/{port}"


def _used_ports(links, node_id):
    used = set()
    for lk in links:
        for e in lk.get("nodes", []):
            if e.get("node_id") == node_id:
                used.add((e.get("adapter_number", 0), e.get("port_number", 0)))
    return used


def _free_port(node, links):
    used = _used_ports(links, node["node_id"])
    for p in _ports_for(node):
        if p not in used:
            return p
    return None


def _link_count(links, node_id):
    return sum(1 for lk in links for e in lk.get("nodes", []) if e.get("node_id") == node_id)


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _find(nodes, name):
    n = _norm(name)
    for node in nodes:
        if _norm(node["name"]) == n:
            return node
    return None


# ───────────────────────────── operations ─────────────────────────────────────

def _next_name(nodes, kind):
    prefix = KIND_PREFIX[kind]
    nums = []
    for n in nodes:
        m = re.fullmatch(rf"{prefix}(\d+)", n["name"], re.I)
        if m:
            nums.append(int(m.group(1)))
    return f"{prefix}{(max(nums) if nums else 0) + 1}"


def _choose_target(nodes, links, kind, connect_to):
    if connect_to:
        n = _find(nodes, connect_to)
        if not n:
            raise OpError(f"I could not find a device called '{connect_to}'.")
        if _free_port(n, links) is None:
            raise OpError(f"{n['name']} has no free port left.")
        return n
    prefs = {"pc": ["switch", "router"], "switch": ["router", "switch"], "router": ["router", "switch"]}[kind]
    for want in prefs:
        cands = [n for n in nodes if _kind(n) == want and _free_port(n, links) is not None]
        if cands:
            fewest = min(_link_count(links, n["node_id"]) for n in cands)
            best = [n for n in cands if _link_count(links, n["node_id"]) == fewest]
            return sorted(best, key=lambda n: g._natural_key(n["name"]))[-1]
    return None


def _place(nodes, target):
    if target:
        bx, by = target["x"], target["y"]
    else:
        bx, by = (max((n["x"] for n in nodes), default=0) + 150), 0
    offsets = [(0, 170), (170, 170), (-170, 170), (170, 0), (-170, 0), (0, -170),
               (340, 170), (-340, 170), (340, 0), (-340, 0), (0, 340), (170, 340), (-170, 340)]
    for dx, dy in offsets:
        x, y = bx + dx, by + dy
        if all(max(abs(x - n["x"]), abs(y - n["y"])) >= 110 for n in nodes):
            return int(x), int(y)
    return int(bx + 400), int(by)


def _new_link(a, a_port, b, b_port):
    return {"link_id": str(uuid.uuid4()),
            "nodes": [{"node_id": a["node_id"], "adapter_number": a_port[0], "port_number": a_port[1],
                       "label": {"text": _port_label(a, *a_port)}},
                      {"node_id": b["node_id"], "adapter_number": b_port[0], "port_number": b_port[1],
                       "label": {"text": _port_label(b, *b_port)}}]}


def _op_add(state, op):
    kind = op.get("kind")
    if kind not in KIND_PREFIX:
        raise OpError("I can add a PC, a router or a switch.")
    count = max(1, min(int(op.get("count") or 1), 6))
    nodes, links = state["nodes"], state["links"]
    for i in range(count):
        target = _choose_target(nodes, links, kind, op.get("connect_to") if i == 0 else None)
        name = _next_name(nodes, kind)
        x, y = _place(nodes, target)
        node = {"node_id": str(uuid.uuid4()), "name": name, "node_type": KIND_NODE_TYPE[kind],
                "x": x, "y": y, "properties": {}}
        nodes.append(node)
        if target:
            tp = _free_port(target, links)
            link = _new_link(target, tp, node, _ports_for(node)[0])
            links.append(link)
            where = f"{target['name']} {link['nodes'][0]['label']['text']}"
            state["changes"].append(f"Added {name} ({kind}) and connected it to {where}.")
        else:
            state["changes"].append(f"Added {name} ({kind}). There was nothing to connect it to yet.")
        state["added"].append(name)


def _remove_node(state, node):
    state["nodes"][:] = [n for n in state["nodes"] if n["node_id"] != node["node_id"]]
    state["links"][:] = [lk for lk in state["links"]
                         if all(e.get("node_id") != node["node_id"] for e in lk.get("nodes", []))]
    if node["name"] in state["added"]:
        state["added"].remove(node["name"])
    state["changes"].append(f"Removed {node['name']} and its links.")


def _op_remove(state, op):
    node = _find(state["nodes"], op.get("name", ""))
    if not node:
        raise OpError(f"I could not find a device called '{op.get('name')}'.")
    _remove_node(state, node)


def _op_remove_last(state, op):
    while state["added"]:
        node = _find(state["nodes"], state["added"][-1])
        if node:
            _remove_node(state, node)
            return
        state["added"].pop()
    raise OpError("Nothing added by the assistant to remove. Say which device to remove, for example 'remove PC4'.")


def _op_connect(state, op):
    a = _find(state["nodes"], op.get("a", ""))
    b = _find(state["nodes"], op.get("b", ""))
    if not a or not b:
        raise OpError("I could not find both devices to connect.")
    if a["node_id"] == b["node_id"]:
        raise OpError("A device cannot be connected to itself.")
    pa, pb = _free_port(a, state["links"]), _free_port(b, state["links"])
    if pa is None or pb is None:
        raise OpError(f"{a['name'] if pa is None else b['name']} has no free port left.")
    link = _new_link(a, pa, b, pb)
    state["links"].append(link)
    state["changes"].append(f"Connected {a['name']} {link['nodes'][0]['label']['text']} "
                            f"to {b['name']} {link['nodes'][1]['label']['text']}.")


OPS = {"add": _op_add, "remove": _op_remove, "remove_last": _op_remove_last, "connect": _op_connect}


# ───────────────────────────── instruction -> operations ──────────────────────

def parse_rules(instruction, nodes):
    t = instruction.strip().lower()
    if re.search(r"\b(?:remove|delete)\s+(?:the\s+)?(?:last|latest)\b", t):
        return [{"op": "remove_last"}]
    m = re.search(r"\b(?:remove|delete)\s+(?:the\s+)?([a-z]+)\s*-?\s*(\d+)\b", t)
    if m:
        name = m.group(1) + m.group(2)
        if not _find(nodes, name):
            raise OpError(f"I could not find a device called '{name.upper() if len(m.group(1)) <= 2 else name.title()}'.")
        return [{"op": "remove", "name": name}]
    m = re.search(r"\bconnect\s+([a-z]+\s*\d+)\s+(?:to|and|with)\s+([a-z]+\s*\d+)", t)
    if m:
        return [{"op": "connect", "a": m.group(1), "b": m.group(2)}]
    if not re.search(r"\b(?:add|create|insert|put|new|more|another|attach|plus)\b", t):
        return []
    to = None
    m = re.search(r"\b(?:to|on|behind|under|from)\s+([a-z]+\s*\d+)\b", t)
    if m and _find(nodes, m.group(1)):
        to = m.group(1)
    ops = []
    for kind, pat in KIND_WORDS.items():
        if not re.search(rf"\b{pat}\b", t):
            continue
        # the thing being connected to ("to Switch1") must not be counted as a request to add it
        if to and kind == g._node_kind(_find(nodes, to).get("node_type")) and \
                len(re.findall(rf"\b{pat}\b", t)) == 1 and re.search(rf"\b{pat}\s*\d", t):
            continue
        count = 1
        c = re.search(rf"\b(\d+|a|an|one|two|three|four|five|another)\s+(?:more\s+|new\s+|extra\s+|additional\s+)?{pat}\b", t)
        if c:
            count = int(c.group(1)) if c.group(1).isdigit() else NUMBER_WORDS.get(c.group(1), 1)
        ops.append({"op": "add", "kind": kind, "count": count})
    if len(ops) == 1 and to:
        ops[0]["connect_to"] = to
    return ops


def parse_ops(instruction, nodes, links):
    ops = parse_rules(instruction, nodes)
    if ops:
        return ops
    try:
        from ai_scanner.gemini_vision import instruction_to_ops
    except Exception as e:  # pragma: no cover
        raise OpError(f"Free-form editing is not available: {e}")
    by_id = {n["node_id"]: n["name"] for n in nodes}
    summary_nodes = ", ".join(f"{n['name']} ({_kind(n)})" for n in nodes)
    summary_links = "; ".join(
        " - ".join(by_id.get(e.get("node_id"), "?") for e in lk.get("nodes", [])) for lk in links)
    try:
        ops = instruction_to_ops(instruction, summary_nodes, summary_links)
    except ValueError as e:
        raise OpError(str(e))
    except Exception as e:
        raise OpError(f"Gemini could not understand the request: {e}")
    clean = [o for o in ops if isinstance(o, dict) and o.get("op") in OPS]
    if not clean:
        raise OpError("I did not understand. Try: 'add a PC', 'add a router', 'add a switch', "
                      "'add a PC to Switch2', 'connect R1 to R3' or 'remove PC4'.")
    return clean


# ───────────────────────────── rebuild the planning model ─────────────────────

def _segments(connections, node_kind):
    parent = {n: n for n in node_kind}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for c in connections:
        a, b = c["from_device"], c["to_device"]
        if "switch" in (node_kind.get(a), node_kind.get(b)) or "pc" in (node_kind.get(a), node_kind.get(b)):
            parent[find(a)] = find(b)
    groups = {}
    for n in node_kind:
        groups.setdefault(find(n), set()).add(n)
    return {n: groups[find(n)] for n in node_kind}


def _fill_missing_pc_ips(devices, connections, node_kind):
    """Keep addresses that already exist; give new PCs the next free host in their segment's subnet."""
    dev = {d["id"]: d for d in devices}
    seg = _segments(connections, node_kind)
    for d in devices:
        if d["type"] != "pc" or not d["interfaces"] or d["interfaces"][0].get("ip"):
            continue
        peers = [dev[p] for p in seg[d["id"]] if p != d["id"]]
        net, gateway = None, ""
        for p in peers:
            for ifc in p["interfaces"]:
                if ifc.get("ip"):
                    try:
                        cand = ipaddress.IPv4Interface(ifc["ip"])
                    except ValueError:
                        continue
                    if net is None:
                        net = cand.network
                    if p["type"] == "router" and cand.network == net and not gateway:
                        gateway = str(cand.ip)
                    elif p["type"] == "pc" and ifc.get("gateway") and not gateway:
                        gateway = ifc["gateway"]
        if net is None:
            continue
        used = {ipaddress.IPv4Interface(i["ip"]).ip for x in devices for i in x["interfaces"] if i.get("ip")}
        for host in net.hosts():
            if host not in used:
                d["interfaces"][0]["ip"] = f"{host}/{net.prefixlen}"
                if gateway:
                    d["interfaces"][0]["gateway"] = gateway
                break


def _rebuild_ai(nodes, links, old_ai, live):
    node_kind = {n["name"]: _kind(n) for n in nodes}
    by_id = {n["node_id"]: n for n in nodes}
    devices = [{"id": n["name"], "label": n["name"], "type": node_kind[n["name"]], "vendor": "cisco",
                "x": n["x"], "y": n["y"], "interfaces": []} for n in nodes]
    dev = {d["id"]: d for d in devices}
    connections = []
    for lk in links:
        sides = []
        for e in lk.get("nodes", []):
            n = by_id.get(e.get("node_id"))
            if not n:
                continue
            label = (e.get("label") or {}).get("text") or _port_label(n, e.get("adapter_number", 0), e.get("port_number", 0))
            if node_kind[n["name"]] == "pc":
                label = "eth0"
            sides.append((n["name"], label))
        if len(sides) != 2:
            continue
        connections.append({"from_device": sides[0][0], "from_interface": sides[0][1],
                            "to_device": sides[1][0], "to_interface": sides[1][1], "link_type": "ethernet"})
        for name, label in sides:
            if node_kind[name] in ("router", "pc"):
                dev[name]["interfaces"].append({"name": label, "ip": ""})

    if live:
        subnets = g._plan_addressing(devices, connections, node_kind)
    else:
        old = {d.get("id"): d for d in (old_ai or {}).get("devices", [])}
        for d in devices:
            for ifc in d["interfaces"]:
                for oi in old.get(d["id"], {}).get("interfaces", []):
                    if oi.get("name") == ifc["name"]:
                        ifc["ip"] = oi.get("ip", "")
                        if oi.get("gateway"):
                            ifc["gateway"] = oi["gateway"]
        _fill_missing_pc_ips(devices, connections, node_kind)
        subnets = (old_ai or {}).get("subnets", [])
    return {"devices": devices, "connections": connections, "subnets": subnets}


def modify(instruction, current):
    """Apply a text instruction to the current topology. Returns (new_data, [change descriptions])."""
    data = copy.deepcopy(current)
    topo = data.get("topology") or {}
    if not topo.get("nodes"):
        raise OpError("There is no topology loaded to modify.")
    for n in topo["nodes"]:
        n.setdefault("properties", {})
        n["x"] = int(n.get("x", 0))
        n["y"] = int(n.get("y", 0))
    state = {"nodes": topo["nodes"], "links": topo.setdefault("links", []),
             "added": list(data.get("_added", [])), "changes": []}
    for op in parse_ops(instruction, state["nodes"], state["links"]):
        OPS[op["op"]](state, op)
    old_ai = data.get("_ai_topology")
    live = data.get("source") == "gns3-live" or not (old_ai or {}).get("devices")
    data["_ai_topology"] = _rebuild_ai(state["nodes"], state["links"], old_ai, live)
    data["_added"] = state["added"]
    return data, state["changes"]


# ───────────────────────────── create the new devices in GNS3 ─────────────────

def _put(path, payload):
    url, auth = g._get_config()
    r = requests.put(f"{url}/v2/{path.lstrip('/')}", json=payload, auth=auth, timeout=20, proxies=g._NO_PROXY)
    r.raise_for_status()
    return r.json() if r.content else {}


def _wait_console_ready(host, port, timeout=170):
    """Wait until a device console shows a prompt (a booting router needs 1-2 minutes)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            s = socket.create_connection((host, port), timeout=5)
            s.settimeout(1.0)
            s.sendall(b"\r\n")
            time.sleep(1.5)
            try:
                out = s.recv(65535).decode("ascii", "replace")
            except socket.timeout:
                out = ""
            if "yes/no" in out.lower() or "initial configuration dialog" in out.lower():
                s.sendall(b"no\r\n")
                time.sleep(2)
            s.close()
            if re.search(r"[#>]\s*$", out.strip()) or "Press RETURN" in out:
                return True
        except OSError:
            pass
        time.sleep(5)
    return False


def sync_new_devices(project_id, ai_devices, connections):
    """Create devices (and their links) that exist in the website model but not in GNS3."""
    nodes = g._get(f"projects/{project_id}/nodes")
    by_name = {n["name"]: n for n in nodes}
    new_devs = [d for d in ai_devices if d.get("id") not in by_name and d.get("type") in KIND_NODE_TYPE]
    if not new_devs:
        return {"ok": True, "created": [], "links": 0, "message": "Nothing to create - GNS3 already has every device."}
    compute = next((n.get("compute_id") for n in nodes if n.get("compute_id")), "vm")
    templates = {t["template_type"]: t for t in g._get("templates")}

    created = []
    for d in new_devs:
        tmpl = templates.get(KIND_NODE_TYPE[d["type"]])
        if not tmpl:
            raise OpError(f"GNS3 has no '{KIND_NODE_TYPE[d['type']]}' template, so {d['id']} cannot be created.")
        node = g._post(f"projects/{project_id}/templates/{tmpl['template_id']}",
                       {"x": int(d.get("x", 0)), "y": int(d.get("y", 0)), "compute_id": tmpl.get("compute_id") or compute},
                       timeout=60)
        if node.get("name") != d["id"]:
            try:
                node = _put(f"projects/{project_id}/nodes/{node['node_id']}", {"name": d["id"]})
            except Exception as e:
                g._log("WARN", f"Could not rename {node.get('name')} to {d['id']}: {e}")
        created.append(d["id"])
        g._log("OK", f"Created {d['id']} ({d['type']}) in GNS3")

    nodes = g._get(f"projects/{project_id}/nodes")
    by_name = {n["name"]: n for n in nodes}
    links = g._get(f"projects/{project_id}/links")
    linked = {frozenset(e["node_id"] for e in lk["nodes"]) for lk in links}
    n_links = 0
    for c in connections:
        a, b = by_name.get(c["from_device"]), by_name.get(c["to_device"])
        if not a or not b or not ({c["from_device"], c["to_device"]} & set(created)):
            continue
        if frozenset([a["node_id"], b["node_id"]]) in linked:
            continue
        ends = []
        for node in (a, b):
            used = {(e["adapter_number"], e["port_number"]) for lk in links for e in lk["nodes"]
                    if e["node_id"] == node["node_id"]}
            free = [p for p in sorted(node.get("ports", []), key=lambda p: (p["adapter_number"], p["port_number"]))
                    if (p["adapter_number"], p["port_number"]) not in used]
            if not free:
                raise OpError(f"{node['name']} has no free port in GNS3.")
            ends.append({"node_id": node["node_id"], "adapter_number": free[0]["adapter_number"],
                         "port_number": free[0]["port_number"]})
        new = g._post(f"projects/{project_id}/links", {"nodes": ends}, timeout=30)
        links.append(new if new.get("nodes") else {"nodes": ends})
        n_links += 1
        g._log("OK", f"Linked {c['from_device']} - {c['to_device']} in GNS3")

    started = [by_name[n] for n in created if n in by_name]
    for node in started:
        g.start_node(project_id, node["node_id"])
    for node in started:
        name = node["name"]
        if node["node_type"] == "dynamips" and node.get("console"):
            g._log("INFO", f"Waiting for {name} to boot...")
            ok = _wait_console_ready(node.get("console_host", "127.0.0.1"), node["console"])
            g._log("OK" if ok else "WARN", f"{name} console {'ready' if ok else 'not ready yet'}")
        else:
            time.sleep(1.5)
    return {"ok": True, "created": created, "links": n_links}
