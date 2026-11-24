"""
gns3_service.py
───────────────
Clean service layer for communicating with a live GNS3 2.2.x server.

Architecture:
  Dashboard Backend (start_dashboard.py)
       ↓
  gns3_service.py  (this file)
       ↓  HTTP REST
  GNS3 Server  (localhost:3080/v2/)
       ↓  Telnet console
  Routers / Switches

All GNS3 credentials and URL are read from environment variables:
  GNS3_SERVER_URL   default: http://localhost:3080
  GNS3_USERNAME     default: (empty — GNS3 2.2 local auth optional)
  GNS3_PASSWORD     default: (empty)
"""

import os
import time
import socket
import threading
import json
import re
import ipaddress
from typing import Optional
from dotenv import load_dotenv

try:
    import telnetlib
    _HAS_TELNETLIB = True
except ImportError:
    _HAS_TELNETLIB = False   # Python 3.13+ removed telnetlib; we use raw sockets instead

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

# Resolve .env relative to this file's own location (ai-engine/.env), not the
# process working directory — critical when called from the HTTP server thread.
_ENV_FILE = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
)

# Persistent activity log (PRD security requirement: configuration activity logging)
_ACTIVITY_LOG = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "activity.log")
)

# Explicit proxy bypass — works reliably on Windows inside HTTP server threads
_NO_PROXY = {"http": None, "https": None}

def _get_config():
    # Bypass any local proxy settings for localhost connection (belt-and-suspenders)
    os.environ["NO_PROXY"]  = "*"
    os.environ["no_proxy"]  = "*"
    os.environ["HTTP_PROXY"]  = ""
    os.environ["HTTPS_PROXY"] = ""
    os.environ["http_proxy"]  = ""
    os.environ["https_proxy"] = ""

    # Only load .env if the key env vars are NOT already set (e.g. by docker-compose).
    # This ensures Docker environment variables always take priority over the .env file,
    # which is critical because the .env file contains 127.0.0.1 (for local runs)
    # while Docker injects host.docker.internal (for container runs).
    if not os.environ.get("GNS3_SERVER_URL"):
        load_dotenv(dotenv_path=_ENV_FILE, override=False)

    url  = os.environ.get("GNS3_SERVER_URL", "http://localhost:3080").rstrip("/")
    user = os.environ.get("GNS3_USERNAME", "").strip()
    pwd  = os.environ.get("GNS3_PASSWORD", "").strip()
    auth = (user, pwd) if user else None
    return url, auth

# ─────────────────────────────────────────────────────────────────────────────
# IN-MEMORY EXECUTION LOG
# ─────────────────────────────────────────────────────────────────────────────

_logs: list[dict] = []
_log_lock = threading.Lock()

def _log(level: str, message: str):
    """Append a structured log entry (thread-safe)."""
    entry = {
        "ts":      time.strftime("%H:%M:%S"),
        "level":   level.upper(),   # OK | INFO | ERROR | WARN
        "message": message,
    }
    with _log_lock:
        _logs.append(entry)
        try:
            os.makedirs(os.path.dirname(_ACTIVITY_LOG), exist_ok=True)
            with open(_ACTIVITY_LOG, "a", encoding="utf-8") as fh:
                one_line = " | ".join(str(message).splitlines())
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{entry['level']}] {one_line}\n")
        except Exception:
            pass
    print(f"[GNS3][{entry['level']}] {message}")

def get_logs() -> list[dict]:
    with _log_lock:
        return list(_logs)

def clear_logs():
    with _log_lock:
        _logs.clear()


# ─────────────────────────────────────────────────────────────────────────────
# GNS3 REST HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _retry_conn(call, tries: int = 4, pause: float = 0.7):
    """Run a requests call; retry short connection hiccups (e.g. a momentary Docker DNS failure)."""
    for attempt in range(tries):
        try:
            return call()
        except requests.exceptions.ConnectionError:
            if attempt == tries - 1:
                raise
            time.sleep(pause)


def _get(path: str, timeout: int = 10) -> dict:
    """GET /v2/{path} — returns parsed JSON or raises RuntimeError."""
    if not REQUESTS_AVAILABLE:
        raise RuntimeError("'requests' library not installed. Run: pip install requests")
    url, auth = _get_config()
    resp = _retry_conn(lambda: requests.get(f"{url}/v2/{path.lstrip('/')}", auth=auth, timeout=timeout, proxies=_NO_PROXY))
    resp.raise_for_status()
    return resp.json()

def _post(path: str, payload: Optional[dict] = None, timeout: int = 10) -> dict:
    """POST /v2/{path} — returns parsed JSON or raises RuntimeError."""
    if not REQUESTS_AVAILABLE:
        raise RuntimeError("'requests' library not installed. Run: pip install requests")
    url, auth = _get_config()
    resp = _retry_conn(lambda: requests.post(
        f"{url}/v2/{path.lstrip('/')}",
        json=payload or {},
        auth=auth,
        timeout=timeout,
        proxies=_NO_PROXY,
    ))
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API — CONNECTION
# ─────────────────────────────────────────────────────────────────────────────

def check_connection() -> dict:
    """
    Verify the GNS3 server is reachable.
    Returns {"ok": True/False, "version": "...", "url": "...", "error": "..."}
    """
    url, auth = _get_config()
    try:
        data = _get("version")
        version = data.get("version", "unknown")
        _log("OK", f"GNS3 server connected — version {version} at {url}")
        return {"ok": True, "version": version, "url": url}
    except Exception as e:
        msg = str(e)
        if "401" in msg or "Unauthorized" in msg:
            cred_hint = f"username='{auth[0]}'" if auth else "no credentials set"
            msg = (f"GNS3 server at {url} rejected credentials ({cred_hint}). "
                   "Check GNS3_USERNAME / GNS3_PASSWORD in .env.")
        elif "Connection refused" in msg or "Failed to establish" in msg:
            msg = f"GNS3 server is not running at {url}. Please start GNS3 and try again. EXACT_ERROR={repr(e)}"
        elif "timeout" in msg.lower():
            msg = f"Connection to GNS3 server timed out. Check that {url} is reachable."
        else:
            msg = f"Unable to connect to GNS3 server at {url}: {msg}"
        _log("ERROR", msg)
        return {"ok": False, "url": url, "error": msg}


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API — PROJECTS
# ─────────────────────────────────────────────────────────────────────────────

def get_projects() -> dict:
    """
    Return all GNS3 projects.
    Returns {"ok": True, "projects": [{"id": ..., "name": ..., "status": ...}, ...]}
    """
    try:
        raw = _get("projects")
        projects = [
            {
                "id":     p["project_id"],
                "name":   p.get("name", "Unnamed"),
                "status": p.get("status", "unknown"),
            }
            for p in raw
        ]
        _log("OK", f"Loaded {len(projects)} GNS3 project(s)")
        return {"ok": True, "projects": projects}
    except Exception as e:
        msg = f"Failed to retrieve GNS3 projects: {e}"
        _log("ERROR", msg)
        return {"ok": False, "error": msg}


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API — NODES
# ─────────────────────────────────────────────────────────────────────────────

def get_project_nodes(project_id: str) -> dict:
    """
    Return all nodes in a GNS3 project.
    Returns {"ok": True, "nodes": [{id, name, status, type, console, console_host}, ...]}
    """
    try:
        raw = _get(f"projects/{project_id}/nodes")
        nodes = []
        for n in raw:
            nodes.append({
                "id":           n["node_id"],
                "node_id":      n["node_id"],
                "project_id":   n["project_id"],
                "name":         n.get("name", ""),
                "status":       n.get("status", "stopped"),
                "node_type":    n.get("node_type", ""),
                "console":      n.get("console"),
                "console_host": n.get("console_host", "127.0.0.1"),
                "console_type": n.get("console_type", "none"),
                "ports":        n.get("ports", []),
            })
        _log("INFO", f"Loaded {len(nodes)} node(s) from project {project_id}")
        return {"ok": True, "nodes": nodes}
    except Exception as e:
        msg = f"Failed to get nodes for project {project_id}: {e}"
        _log("ERROR", msg)
        return {"ok": False, "error": msg}


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API — NODE CONTROL
# ─────────────────────────────────────────────────────────────────────────────

def start_node(project_id: str, node_id: str) -> dict:
    """Start a GNS3 node. Returns {"ok": True/False, "error": ...}"""
    try:
        _post(f"projects/{project_id}/nodes/{node_id}/start")
        _log("OK", f"Node {node_id} started")
        return {"ok": True}
    except Exception as e:
        msg = f"Failed to start node {node_id}: {e}"
        _log("ERROR", msg)
        return {"ok": False, "error": msg}

def stop_node(project_id: str, node_id: str) -> dict:
    """Stop a GNS3 node. Returns {"ok": True/False, "error": ...}"""
    try:
        _post(f"projects/{project_id}/nodes/{node_id}/stop")
        _log("OK", f"Node {node_id} stopped")
        return {"ok": True}
    except Exception as e:
        msg = f"Failed to stop node {node_id}: {e}"
        _log("ERROR", msg)
        return {"ok": False, "error": msg}

def ensure_node_running(project_id: str, node_id: str, node_name: str) -> dict:
    """
    Check if a node is started; if not, start it automatically and wait up to
    30 s for it to come online. Returns {"ok": True/False, "error": ...}.
    """
    try:
        node_info = _get(f"projects/{project_id}/nodes/{node_id}")
        status = node_info.get("status", "stopped")

        if status == "started":
            _log("INFO", f"{node_name} is already running")
            return {"ok": True}

        _log("INFO", f"{node_name} is stopped — starting automatically…")
        result = start_node(project_id, node_id)
        if not result["ok"]:
            return result

        # Wait for node to become started (up to 30 s)
        for _ in range(15):
            time.sleep(2)
            info = _get(f"projects/{project_id}/nodes/{node_id}")
            if info.get("status") == "started":
                _log("OK", f"{node_name} is now running")
                return {"ok": True}

        _log("WARN", f"{node_name} did not confirm started within 30 s — continuing anyway")
        return {"ok": True}

    except Exception as e:
        msg = f"Could not ensure {node_name} is running: {e}"
        _log("ERROR", msg)
        return {"ok": False, "error": msg}


# ─────────────────────────────────────────────────────────────────────────────
# TELNET CONSOLE
# ─────────────────────────────────────────────────────────────────────────────

def _telnet_send_commands(host: str, port: int, commands: list[str],
                           timeout: int = 60) -> dict:
    """
    Open a Telnet session to a GNS3 node console, send IOS commands, and
    capture output. Works on Python 3.9-3.14 (telnetlib removed in 3.13).
    Returns {"ok": True, "output": "..."} or {"ok": False, ...}.
    """
    if _HAS_TELNETLIB:
        return _telnet_via_telnetlib(host, port, commands, timeout)
    else:
        return _telnet_via_socket(host, port, commands, timeout)


def _telnet_via_telnetlib(host, port, commands, timeout):
    """Telnet using the standard telnetlib (Python <= 3.12)."""
    output_lines = []
    try:
        _log("INFO", f"Opening Telnet (telnetlib) to {host}:{port} …")
        tn = telnetlib.Telnet(host, port, timeout=timeout)

        def read_prompt(wait=3.0):
            patterns = [b"#", b">", b"--More--", b"Press RETURN", b"$"]
            deadline = time.time() + wait
            buf = b""
            while time.time() < deadline:
                try:
                    chunk = tn.read_eager()
                    if chunk:
                        buf += chunk
                        for p in patterns:
                            # Avoid matching # inside a syslog message if possible
                            if p in buf and not buf.endswith(b"up\r\n"): 
                                return buf
                except EOFError:
                    break
                time.sleep(0.1)
            return buf

        # Wake up the console
        tn.write(b"\r\n\r\n")
        time.sleep(0.5)
        tn.read_very_eager() # Clear buffer
        tn.write(b"\r\n")
        read_prompt(wait=2.0)
        
        for cmd in commands:
            tn.write((cmd + "\r\n").encode("ascii"))
            # Wait longer for interface and no shutdown commands
            if cmd.startswith("interface") or cmd.startswith("no shut"):
                time.sleep(0.8)
            else:
                time.sleep(0.3)
            out = read_prompt(wait=3.0)
            output_lines.append(out.decode("ascii", errors="replace").strip())

        tn.close()
        _log("INFO", f"Telnet Output for {host}:{port}:\n{chr(10).join(output_lines)}")
        _log("OK", f"Commands sent via telnetlib to {host}:{port}")
        return {"ok": True, "output": "\n".join(output_lines)}
    except ConnectionRefusedError:
        msg = f"Telnet refused on {host}:{port}. Console may not be ready."
        _log("ERROR", msg); return {"ok": False, "error": msg}
    except socket.timeout:
        msg = f"Telnet to {host}:{port} timed out after {timeout}s."
        _log("ERROR", msg); return {"ok": False, "error": msg}
    except Exception as e:
        msg = f"Telnet error on {host}:{port}: {e}"
        _log("ERROR", msg); return {"ok": False, "error": msg}


# IAC codes for raw-socket Telnet negotiation
_IAC  = bytes([255])
_DONT = bytes([254])
_DO   = bytes([253])
_WONT = bytes([252])
_WILL = bytes([251])

def _telnet_via_socket(host, port, commands, timeout):
    """Telnet using raw sockets (Python 3.13+ where telnetlib was removed)."""
    output_lines = []
    try:
        _log("INFO", f"Opening Telnet (socket) to {host}:{port} …")
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)

        def recv_until_prompt(wait=3.0):
            """Read bytes until a common IOS prompt appears or wait expires."""
            buf = b""
            deadline = time.time() + wait
            while time.time() < deadline:
                try:
                    sock.settimeout(0.15)
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    # Strip Telnet IAC sequences
                    i = 0
                    clean = bytearray()
                    while i < len(chunk):
                        if chunk[i:i+1] == _IAC and i + 2 < len(chunk):
                            cmd_byte = chunk[i+1:i+2]
                            # Respond to DO/WILL with WONT/DONT
                            if cmd_byte in (_DO, _WILL):
                                sock.sendall(_IAC + _WONT + chunk[i+2:i+3])
                            i += 3
                        else:
                            clean.append(chunk[i])
                            i += 1
                    buf += bytes(clean)
                    # Check for IOS prompt indicators
                    for p in [b"#", b">", b"$", b"--More--"]:
                        if p in buf:
                            return buf
                except socket.timeout:
                    pass
                except Exception:
                    break
            return buf

        # Wake up the console and wait for initial banner
        sock.sendall(b"\r\n\r\n")
        out = recv_until_prompt(wait=3.0)
        
        # If the router asks for initial configuration dialog, say no and wait for boot
        if b"yes/no" in out.lower() or b"initial configuration dialog" in out.lower():
            sock.sendall(b"no\r\n")
            _log("INFO", f"Answering 'no' to initial config dialog on {host}:{port} and waiting for boot...")
            time.sleep(5.0)  # Give it some time to decompress/boot
            recv_until_prompt(wait=10.0)
            sock.sendall(b"\r\n\r\n")
            out = recv_until_prompt(wait=3.0)

        for cmd in commands:
            sock.sendall((cmd + "\r\n").encode("ascii"))
            time.sleep(0.3)
            out = recv_until_prompt(wait=4.0)
            output_lines.append(out.decode("ascii", errors="replace").strip())

        sock.close()
        _log("OK", f"Commands sent via socket-telnet to {host}:{port}")
        return {"ok": True, "output": "\n".join(output_lines)}

    except ConnectionRefusedError:
        msg = f"Telnet refused on {host}:{port}. Console may not be ready."
        _log("ERROR", msg); return {"ok": False, "error": msg}
    except socket.timeout:
        msg = f"Telnet to {host}:{port} timed out after {timeout}s."
        _log("ERROR", msg); return {"ok": False, "error": msg}
    except Exception as e:
        msg = f"Telnet socket error on {host}:{port}: {e}"
        _log("ERROR", msg); return {"ok": False, "error": msg}


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG GENERATION FROM AI TOPOLOGY
# ─────────────────────────────────────────────────────────────────────────────

def _cidr_to_mask(cidr_or_ip: str) -> tuple[str, str]:
    """Convert '192.168.1.1/24' → ('192.168.1.1', '255.255.255.0')."""
    try:
        if "/" in cidr_or_ip:
            iface = ipaddress.IPv4Interface(cidr_or_ip)
            return str(iface.ip), str(iface.netmask)
        return cidr_or_ip, "255.255.255.0"
    except Exception:
        return cidr_or_ip, "255.255.255.0"


def _normalize_iface_name(raw: str) -> str:
    """
    Normalize short interface names to full IOS names.
    g0/0 → GigabitEthernet0/0, f0/0 → FastEthernet0/0, e0 → Ethernet0, etc.
    """
    raw = raw.strip()
    lraw = raw.lower()
    if lraw.startswith("gig") or lraw.startswith("g"):
        rest = re.sub(r"^g(?:iga)?(?:bit)?(?:ethernet)?", "", lraw)
        return f"GigabitEthernet{rest}"
    if lraw.startswith("fa") or re.match(r"^f\d", lraw):
        rest = re.sub(r"^f(?:ast)?(?:ethernet)?", "", lraw)
        return f"FastEthernet{rest}"
    if lraw.startswith("eth"):
        rest = re.sub(r"^eth(?:ernet)?", "", lraw)
        return f"Ethernet{rest}"
    if lraw.startswith("e") and not lraw.startswith("eth"):
        rest = re.sub(r"^e", "", lraw)
        return f"Ethernet{rest}"
    if lraw.startswith("ser") or lraw.startswith("s"):
        rest = re.sub(r"^s(?:er(?:ial)?)?", "", lraw)
        return f"Serial{rest}"
    return raw  # unknown — keep as-is


def _map_ai_ifaces_to_hw(ai_interfaces: list, gns3_node: dict, ai_connections: list = None) -> list[dict]:
    """
    Map AI interface list to actual hardware port names from GNS3.
    Uses an advanced graph-based mapping: cross-references the AI's intended logical
    connections against the actual physical GNS3 wiring to correct "backwards" wiring.
    Falls back to index-based mapping if graph mapping fails.
    """
    if not gns3_node:
        return [{"hw_name": _normalize_iface_name(i.get("name", "")), "ai_name": i.get("name", ""),
                 "ip": i.get("ip", ""), "gateway": i.get("gateway", "")}
                for i in ai_interfaces]

    hw_ports_sorted = sorted(
        gns3_node.get("ports", []),
        key=lambda p: (p.get("adapter_number", 0), p.get("port_number", 0))
    )

    target_to_gns3_port = {}
    ai_iface_to_target = {}

    # Attempt advanced graph mapping if connections are provided
    if ai_connections:
        try:
            pid = gns3_node.get("project_id")
            my_gns3_id = gns3_node.get("node_id")
            my_name = gns3_node.get("name")
            
            # 1. Build AI Logical Map (Iface -> Target Name)
            for conn in ai_connections:
                if conn.get('from_device') == my_name:
                    ai_iface_to_target[conn.get('from_interface')] = conn.get('to_device')
                elif conn.get('to_device') == my_name:
                    ai_iface_to_target[conn.get('to_interface')] = conn.get('from_device')

            # 2. Build GNS3 Physical Map (Port -> Target Name)
            if pid and my_gns3_id:
                gns3_links = _get(f"projects/{pid}/links")
                gns3_nodes = _get(f"projects/{pid}/nodes")
                gns3_nodes_by_id = {n["node_id"]: n for n in gns3_nodes}

                for lk in gns3_links:
                    n1, n2 = lk['nodes'][0], lk['nodes'][1]
                    if n1['node_id'] == my_gns3_id:
                        my_idx = (n1['adapter_number'], n1['port_number'])
                        tgt_id = n2['node_id']
                    elif n2['node_id'] == my_gns3_id:
                        my_idx = (n2['adapter_number'], n2['port_number'])
                        tgt_id = n1['node_id']
                    else:
                        continue

                    my_port_name = None
                    for p in gns3_node.get("ports", []):
                        if (p['adapter_number'], p['port_number']) == my_idx:
                            my_port_name = p['name']
                            break

                    tgt_name = gns3_nodes_by_id.get(tgt_id, {}).get("name")
                    if my_port_name and tgt_name:
                        target_to_gns3_port[tgt_name] = my_port_name

        except Exception as e:
            _log("WARN", f"Advanced graph mapping failed for {gns3_node.get('name')}: {e}")

    result = []
    for idx, ai_iface in enumerate(ai_interfaces):
        hw_name = None
        # Try advanced mapping first
        ai_iname = ai_iface.get("name")
        target = ai_iface_to_target.get(ai_iname)
        if target and target in target_to_gns3_port:
            hw_name = target_to_gns3_port[target]
        else:
            # Fallback to index-based mapping
            if idx < len(hw_ports_sorted):
                hw_name = hw_ports_sorted[idx].get("name", "")
            else:
                hw_name = _normalize_iface_name(ai_iname)

        result.append({
            "hw_name": hw_name,
            "ai_name": ai_iname,
            "ip":      ai_iface.get("ip", ""),
            "gateway": ai_iface.get("gateway", ""),
        })
    return result


def _build_static_routes(device: dict, all_devices: list) -> list[str]:
    """
    Generate static route commands for a router.
    Finds routers reachable via shared transit subnets and adds routes
    to their non-transit subnets through the appropriate next-hop.
    """
    my_label = device.get("label") or device.get("id", "")
    my_nets: list = []
    my_ifc_objs: list = []
    for iface in device.get("interfaces", []):
        ip_str = iface.get("ip", "")
        if not ip_str:
            continue
        try:
            ifc = ipaddress.IPv4Interface(ip_str)
            my_nets.append(ifc.network)
            my_ifc_objs.append(ifc)
        except ValueError:
            pass

    if not my_nets:
        return []

    route_cmds = []
    seen: set = set()
    for other in all_devices:
        other_label = other.get("label") or other.get("id", "")
        if other_label == my_label:
            continue
        if other.get("type", "").lower() not in ("router",):
            continue
        for other_iface in other.get("interfaces", []):
            try:
                other_ifc = ipaddress.IPv4Interface(other_iface.get("ip", "") or "")
            except ValueError:
                continue
            # Detect transit: other router is on one of my directly-connected subnets
            for my_ifc in my_ifc_objs:
                if other_ifc.network != my_ifc.network:
                    continue
                next_hop = str(other_ifc.ip)
                # Add routes to all other router's subnets not directly connected to me
                for rem_iface in other.get("interfaces", []):
                    try:
                        rem_ifc = ipaddress.IPv4Interface(rem_iface.get("ip", "") or "")
                    except ValueError:
                        continue
                    if rem_ifc.network in my_nets:
                        continue
                    key = f"{rem_ifc.network}/{next_hop}"
                    if key in seen:
                        continue
                    seen.add(key)
                    net_addr = str(rem_ifc.network.network_address)
                    net_mask = str(rem_ifc.network.netmask)
                    route_cmds.append(f"ip route {net_addr} {net_mask} {next_hop}")
    return route_cmds


def generate_device_commands(device: dict, gns3_node: dict = None,
                             all_devices: list = None,
                             connections: list = None) -> list[str]:
    """
    Generate Cisco IOS configuration commands for a device.
    - gns3_node: when supplied, interface names match real hardware ports.
    - all_devices: when supplied, static routes are auto-generated for routers.
    """
    name = device.get("label") or device.get("id") or "Device"
    device_type = device.get("type", "router").lower()
    interfaces = device.get("interfaces", [])

    # Detect GNS3 node type to handle special cases
    gns3_node_type = (gns3_node or {}).get("node_type", "").lower() if gns3_node else ""

    # Cloud / NAT nodes — no IOS console, skip
    if device_type in ("cloud", "nat") or gns3_node_type == "cloud":
        return []

    # VPCS / PC nodes — VPCS command syntax
    if device_type in ("pc", "vpcs") or gns3_node_type == "vpcs":
        cmds = []
        if interfaces:
            iface = interfaces[0]
            ip_str = iface.get("ip", "")
            gw_str = iface.get("gateway", "")
            if ip_str and ip_str not in ("0.0.0.0", "0.0.0.0/0", ""):
                if gw_str and gw_str not in ("0.0.0.0", ""):
                    cmds.append(f"ip {ip_str} {gw_str}")
                else:
                    cmds.append(f"ip {ip_str}")
                cmds.append("save")
        return cmds

    # GNS3 unmanaged ethernet_switch — no IOS CLI, skip switchport commands
    if gns3_node_type == "ethernet_switch":
        return []

    # Managed switch with IOS
    if device_type == "switch":
        mapped = _map_ai_ifaces_to_hw(interfaces, gns3_node, connections)
        cmds = ["end", "enable", "configure terminal", f"hostname {name}"]
        for m in mapped:
            hw = m["hw_name"]
            if hw:
                cmds += [f"interface {hw}", "no shutdown", "exit"]
        cmds += ["end", "write memory"]
        return cmds

    # Router (default) — map AI interface names to real hardware port names
    mapped = _map_ai_ifaces_to_hw(interfaces, gns3_node, connections)
    cmds = ["end", "enable", "configure terminal", f"hostname {name}"]

    # Who is on the other end of each interface (used for descriptions)
    peers = {}
    for conn in (connections or []):
        if conn.get("from_device") == name:
            peers[conn.get("from_interface")] = conn.get("to_device")
        elif conn.get("to_device") == name:
            peers[conn.get("to_interface")] = conn.get("from_device")

    routed_nets = []
    for m in mapped:
        ip_str = m["ip"]
        hw     = m["hw_name"]
        if not hw:
            continue
        # Skip invalid/placeholder IPs
        if ip_str and ip_str.split("/")[0] in ("0.0.0.0", ""):
            ip_str = ""
        cmds.append(f"interface {hw}")
        peer = peers.get(m.get("ai_name"))
        if peer:
            cmds.append(f"description Link to {peer}")
        if ip_str:
            ip, mask = _cidr_to_mask(ip_str)
            # Clear stale IPs from previous (incorrect) apply runs
            cmds += ["no ip address", f"ip address {ip} {mask}", "no shutdown"]
            try:
                routed_nets.append(ipaddress.IPv4Interface(ip_str).network)
            except ValueError:
                pass
        else:
            cmds.append("no shutdown")
        cmds.append("exit")

    if all_devices:
        # Static routes written by earlier runs are removed; OSPF provides the routes now.
        for route in _build_static_routes(device, all_devices):
            cmds.append("no " + route)

        other_routers = [d for d in all_devices
                         if d.get("type", "").lower() == "router"
                         and (d.get("label") or d.get("id")) != name]
        if other_routers and routed_nets:
            cmds.append("router ospf 1")
            for net in routed_nets:
                cmds.append(f"network {net.network_address} {net.hostmask} area 0")
            cmds.append("exit")

    cmds += ["end", "write memory"]
    return cmds


def validate_device_config(device: dict) -> list[str]:
    """
    Validate AI-generated device config before sending to GNS3.
    Returns a list of warning/error strings. Empty list = valid.
    """
    warnings = []
    name = device.get("label") or device.get("id") or ""
    if not name:
        warnings.append("Device has no name (id/label)")

    device_type = device.get("type", "").lower()
    valid_types = {"router", "switch", "pc", "vpcs", "firewall", "server",
                   "wireless", "cloud", "unknown"}
    if device_type not in valid_types:
        warnings.append(f"Unknown device type: '{device_type}'")

    for iface in device.get("interfaces", []):
        ip_str = iface.get("ip", "")
        if ip_str:
            try:
                ipaddress.IPv4Interface(ip_str)
            except ValueError:
                warnings.append(
                    f"{name}: Invalid IP/mask '{ip_str}' on interface "
                    f"'{iface.get('name', '?')}'"
                )

    return warnings


# ─────────────────────────────────────────────────────────────────────────────
# DEVICE MAPPING
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_name(name: str) -> str:
    """Lower-case and strip common non-alphanumeric prefixes for fuzzy match."""
    n = name.lower().strip()
    n = re.sub(r"[^a-z0-9]", "", n)   # keep only alphanumeric
    return n


def auto_map_devices(ai_devices: list[dict], gns3_nodes: list[dict]) -> list[dict]:
    """
    Attempt to auto-map AI devices to GNS3 nodes by name similarity.
    Returns a mapping list:
    [
      {"ai_name": "R1", "gns3_id": "uuid...", "gns3_name": "R1", "confidence": "exact"},
      {"ai_name": "R2", "gns3_id": None,      "gns3_name": None, "confidence": "none"},
    ]
    """
    mapping = []
    gns3_by_norm = {_normalize_name(n["name"]): n for n in gns3_nodes}

    for dev in ai_devices:
        ai_name = dev.get("label") or dev.get("id") or ""
        norm_ai = _normalize_name(ai_name)

        # 1. Exact normalized match
        if norm_ai in gns3_by_norm:
            gn = gns3_by_norm[norm_ai]
            mapping.append({
                "ai_name":   ai_name,
                "gns3_id":   gn["id"],
                "gns3_name": gn["name"],
                "confidence": "exact",
            })
            continue

        # 2. Substring match (e.g., "Router_R1" → "R1")
        found = None
        for norm_gns3, gn in gns3_by_norm.items():
            if norm_ai in norm_gns3 or norm_gns3 in norm_ai:
                found = gn
                break

        if found:
            mapping.append({
                "ai_name":   ai_name,
                "gns3_id":   found["id"],
                "gns3_name": found["name"],
                "confidence": "fuzzy",
            })
        else:
            mapping.append({
                "ai_name":   ai_name,
                "gns3_id":   None,
                "gns3_name": None,
                "confidence": "none",
            })

    return mapping


# ─────────────────────────────────────────────────────────────────────────────
# MAIN APPLY WORKFLOW
# ─────────────────────────────────────────────────────────────────────────────

def apply_configuration(project_id: str, device_mapping: list[dict],
                        ai_devices: list[dict], gns3_nodes: list[dict], topology_data: dict = None) -> dict:
    """
    Full automated workflow:
      1. Validate all AI-generated configs
      2. For each mapped device:
         a. Ensure node is running (auto-start)
         b. Open Telnet console
         c. Send configuration commands
         d. Collect output
      3. Return per-device results

    Args:
        project_id:     GNS3 project UUID
        device_mapping: [{ai_name, gns3_id, gns3_name, confidence}, ...]
        ai_devices:     Full AI device list from scanned topology
        gns3_nodes:     Full GNS3 nodes list from get_project_nodes()

    Returns:
        {"ok": True/False, "results": [{device, status, output, error}, ...]}
    """
    clear_logs()
    _log("INFO", "Starting configuration apply workflow…")

    # Index for fast lookup
    ai_by_name  = {(d.get("label") or d.get("id", "")): d for d in ai_devices}
    gns3_by_id  = {n["id"]: n for n in gns3_nodes}

    results = []
    all_ok  = True

    # ── Phase 1: Validate ────────────────────────────────────────────────────
    _log("INFO", "Phase 1: Validating AI-generated configurations…")
    for mapping in device_mapping:
        ai_name  = mapping["ai_name"]
        gns3_id  = mapping.get("gns3_id")
        ai_dev   = ai_by_name.get(ai_name, {})

        if not gns3_id:
            msg = (f"AI device '{ai_name}' has no GNS3 mapping. "
                   "Please select a GNS3 device manually.")
            _log("ERROR", msg)
            results.append({
                "device": ai_name, "status": "error",
                "output": "", "error": msg,
            })
            all_ok = False
            continue

        warnings = validate_device_config(ai_dev)
        for w in warnings:
            _log("WARN", w)

        if not ai_dev.get("interfaces") and ai_dev.get("type", "").lower() not in ("switch", "pc", "vpcs"):
            _log("WARN", f"{ai_name}: No interfaces found — will only set hostname")

    # ── Phase 2: Apply per device ────────────────────────────────────────────
    _log("INFO", "Phase 2: Sending configurations to GNS3 devices…")
    for mapping in device_mapping:
        ai_name  = mapping["ai_name"]
        gns3_id  = mapping.get("gns3_id")
        if not gns3_id:
            continue   # already logged above

        gns3_node = gns3_by_id.get(gns3_id, {})
        gns3_name = gns3_node.get("name", gns3_id)
        ai_dev    = ai_by_name.get(ai_name, {})

        _log("INFO", f"Processing: AI '{ai_name}' -> GNS3 '{gns3_name}'")

        # 2a. Ensure node is running
        ensure_result = ensure_node_running(project_id, gns3_id, gns3_name)
        if not ensure_result["ok"]:
            results.append({
                "device": ai_name, "status": "error",
                "output": "", "error": ensure_result["error"],
            })
            all_ok = False
            continue

        # 2a. Attempt to get connections from passed data, fallback to scanned.gns3
        connections = (topology_data or {}).get("_ai_topology", {}).get("connections", [])
        if not connections:
            try:
                import os, json
                if os.path.exists('topology/scanned.gns3'):
                    with open('topology/scanned.gns3', 'r') as f:
                        scanned_top = json.load(f)
                        connections = scanned_top.get("_ai_topology", {}).get("connections", [])
            except Exception as e:
                _log("WARN", f"Failed to load connections from scanned.gns3: {e}")

        # 2b. Build commands
        commands = generate_device_commands(
            ai_dev, 
            gns3_node=gns3_node, 
            all_devices=ai_devices, 
            connections=connections
        )
        if not commands:
            _log("INFO", f"{gns3_name}: nothing to configure (unmanaged switch / no console config) — skipped")
            results.append({
                "device": ai_name, "status": "skipped",
                "output": "No configuration needed for this device (unmanaged switch).",
                "error": "",
            })
            continue

        _log("INFO", f"{gns3_name}: Sending {len(commands)} command(s) via Telnet…")

        # 2c. Send via Telnet
        console_host = gns3_node.get("console_host", "127.0.0.1")
        console_port = gns3_node.get("console")
        console_type = gns3_node.get("console_type", "none")

        if not console_port or console_type == "none":
            msg = (f"{gns3_name}: No console port available "
                   f"(type={console_type}). Cannot send configuration.")
            _log("WARN", msg)
            results.append({
                "device": ai_name, "status": "skipped",
                "output": msg, "error": "",
            })
            continue

        telnet_result = _telnet_send_commands(
            console_host, console_port, commands, timeout=60
        )

        if telnet_result["ok"]:
            _log("OK", f"{gns3_name}: Configuration applied successfully")
            results.append({
                "device":  ai_name,
                "status":  "success",
                "output":  telnet_result.get("output", ""),
                "error":   "",
                "commands": commands,
            })
        else:
            all_ok = False
            results.append({
                "device":  ai_name,
                "status":  "error",
                "output":  "",
                "error":   telnet_result.get("error", "Unknown telnet error"),
                "commands": commands,
            })

    _log("INFO", "Configuration apply workflow complete.")
    return {"ok": all_ok, "results": results}


# ─────────────────────────────────────────────────────────────────────────────
# VERIFICATION
# ─────────────────────────────────────────────────────────────────────────────
# Commands to run to "verify" the config worked
_VERIFY_COMMANDS = {
    "router":   ["show ip interface brief", "show ip ospf neighbor", "show ip route"],
    "switch":   ["show ip interface brief", "show vlan brief"],
    "pc":       ["show ip"],
    "vpcs":     ["show ip"],
    "default":  ["show ip interface brief"],
}

def _parse_router_output(raw_output):
    parsed = {}
    for line in raw_output.strip().split('\n'):
        match = re.search(r'^(\S+)\s+(\S+)\s+\w+\s+\w+\s+(administratively down|up|down)\s+(up|down)', line.strip())
        if match:
            parsed[match.group(1)] = {
                "ip": match.group(2),
                "status": match.group(3),
                "protocol": match.group(4)
            }
    return parsed

def _parse_pc_output(raw_output):
    parsed = {}
    ip_match = re.search(r'IP/MASK\s+:\s+(\S+)', raw_output)
    gw_match = re.search(r'GATEWAY\s+:\s+(\S+)', raw_output)
    if ip_match:
        # Strip CIDR prefix (e.g. "192.168.1.10/24" -> "192.168.1.10")
        parsed["ip"] = ip_match.group(1).split("/")[0]
    if gw_match:
        parsed["gateway"] = gw_match.group(1).split("/")[0]
    return parsed

def verify_configuration(project_id: str, device_mapping: list[dict],
                          ai_devices: list[dict], gns3_nodes: list[dict]) -> dict:
    """
    Run verification commands on each mapped GNS3 device and return output.
    Returns {"ok": True, "results": [{device, status, output}, ...]}
    """
    _log("INFO", "Starting post-apply verification…")
    ai_by_name = {(d.get("label") or d.get("id", "")): d for d in ai_devices}
    gns3_by_id = {n["id"]: n for n in gns3_nodes}
    results    = []

    for mapping in device_mapping:
        ai_name  = mapping["ai_name"]
        gns3_id  = mapping.get("gns3_id")
        if not gns3_id:
            results.append({"device": ai_name, "status": "skipped",
                             "output": "No GNS3 mapping", "error": ""})
            continue

        gns3_node = gns3_by_id.get(gns3_id, {})
        gns3_name = gns3_node.get("name", gns3_id)
        ai_dev    = ai_by_name.get(ai_name, {})
        dev_type  = ai_dev.get("type", "default").lower()

        verify_cmds = _VERIFY_COMMANDS.get(dev_type, _VERIFY_COMMANDS["default"])
        _log("INFO", f"Verifying {gns3_name} with: {verify_cmds}")

        console_host = gns3_node.get("console_host", "127.0.0.1")
        console_port = gns3_node.get("console")
        console_type = gns3_node.get("console_type", "none")

        if not console_port or console_type == "none":
            results.append({"device": ai_name, "status": "skipped",
                             "output": f"No console port on {gns3_name}", "error": ""})
            continue

        # Need to enter enable first for verification
        cmds = ["enable"] + verify_cmds
        r = _telnet_send_commands(console_host, console_port, cmds, timeout=60)
        
        parsed_summary = []
        error_msg = ""
        
        if r["ok"]:
            raw_output = r.get("output", "")
            
            if dev_type in ["router", "switch"]:
                parsed = _parse_router_output(raw_output)
                needs_fix = [iface for iface, d in parsed.items() if d["status"] == "administratively down"]
                if needs_fix:
                    _log("INFO", f"Auto-correcting administratively down interfaces on {gns3_name}: {needs_fix}")
                    fix_cmds = ["configure terminal"]
                    for iface in needs_fix:
                        fix_cmds.extend([f"interface {iface}", "no shutdown", "exit"])
                    fix_cmds.extend(["end", "write memory"] + verify_cmds)
                    fix_r = _telnet_send_commands(console_host, console_port, fix_cmds, timeout=60)
                    if fix_r["ok"]:
                        raw_output = fix_r.get("output", "")
                        parsed = _parse_router_output(raw_output)
                
                # Hardware to AI mapping for validation
                hardware_to_ai = {}
                gns3_ports = sorted(gns3_node.get("ports", []), key=lambda p: (p.get("adapter_number", 0), p.get("port_number", 0)))
                for i, ai_iface in enumerate(ai_dev.get("interfaces", [])):
                    if i < len(gns3_ports):
                        hw_name = gns3_ports[i].get("name", "")
                        hardware_to_ai[hw_name] = ai_iface
                        hardware_to_ai[_normalize_iface_name(hw_name)] = ai_iface
                
                all_ok = True
                for iface, data in parsed.items():
                    # Only report interfaces that are mapped or have IPs
                    ai_iface = hardware_to_ai.get(iface)
                    if ai_iface:
                        expected_ip_cidr = ai_iface.get("ip", "")
                        expected_ip = expected_ip_cidr.split("/")[0] if expected_ip_cidr else ""
                        
                        if expected_ip and data["ip"] != expected_ip:
                            error_msg += f"{ai_name} {iface} configuration failed.\nExpected IP: {expected_ip}\nActual IP: {data['ip']}\n"
                            all_ok = False
                        elif expected_ip and data["protocol"] == "down":
                            error_msg += f"{ai_name} {iface} configured successfully, but interface protocol is down.\nCheck the GNS3 link connection.\n"
                            all_ok = False
                            
                        parsed_summary.append(f"{iface} → {expected_ip_cidr or data['ip']}     {data['status'].upper()}/{data['protocol'].upper()}")
                    elif data["ip"] != "unassigned":
                        # Always show configured IPs even if not in the AI plan
                        parsed_summary.append(f"{iface} → {data['ip']}     {data['status'].upper()}/{data['protocol'].upper()} (Unmapped)")
                
                if not all_ok:
                    results.append({"device": ai_name, "status": "error", "output": raw_output, "error": error_msg, "parsed_summary": parsed_summary})
                    continue
                    
            elif dev_type in ["pc", "vpcs"]:
                parsed = _parse_pc_output(raw_output)
                if "ip" in parsed:
                    parsed_summary.append(f"{parsed['ip']}")
                if "gateway" in parsed:
                    parsed_summary.append(f"Gateway: {parsed['gateway']}")

            _log("OK", f"{gns3_name}: Verification complete")
            results.append({"device": ai_name, "status": "success",
                             "output": raw_output, "error": "", "parsed_summary": parsed_summary})
        else:
            results.append({"device": ai_name, "status": "error",
                             "output": "", "error": r.get("error", "")})

    return {"ok": True, "results": results}


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG PREVIEW (for UI display before applying)
# ─────────────────────────────────────────────────────────────────────────────

def build_config_preview(ai_devices: list[dict], connections: list = None) -> dict:
    """
    Generate a human-readable configuration preview for all AI devices.
    Returns {"ok": True, "preview": {"R1": ["cmd1", ...], ...}, "warnings": [...]}
    """
    preview  = {}
    warnings = []
    for dev in ai_devices:
        name  = dev.get("label") or dev.get("id") or "Unknown"
        cmds  = generate_device_commands(dev, all_devices=ai_devices, connections=connections)
        warns = validate_device_config(dev)
        preview[name] = cmds
        if warns:
            warnings.extend(warns)
    return {"ok": True, "preview": preview, "warnings": warnings}


# ─────────────────────────────────────────────────────────────────────────────
# LIVE IP SYNCHRONIZATION
# ─────────────────────────────────────────────────────────────────────────────

import concurrent.futures

def get_live_ips(project_id: str) -> dict:
    """
    Connect to all running nodes in the project and extract their live IP addresses.
    Returns {"ok": True, "live_ips": {"R1": "192.168.10.1", ...}}
    """
    _log("INFO", f"Fetching live IPs for project {project_id}...")
    nodes_res = get_project_nodes(project_id)
    if not nodes_res.get("ok"):
        return nodes_res
        
    nodes = nodes_res.get("nodes", [])
    live_ips = {}
    
    def fetch_ip_for_node(node):
        if node.get("status") != "started":
            return None, None
            
        name = node.get("name")
        console_host = node.get("console_host", "127.0.0.1")
        console_port = node.get("console")
        if not console_port:
            return None, None
            
        node_type = node.get("node_type", "dynamips").lower()
        if "switch" in node_type or node_type in ("ethernet_hub", "cloud", "nat"):
            return None, None
        if node_type == "vpcs" or name.startswith("PC"):
            # PC Node
            r = _telnet_send_commands(console_host, console_port, ["show ip"], timeout=15)
            if r["ok"]:
                parsed = _parse_pc_output(r["output"])
                if "ip" in parsed:
                    return name, parsed["ip"]
                return name, "unassigned"
        else:
            # Router/Switch Node
            r = _telnet_send_commands(console_host, console_port, ["show ip interface brief"], timeout=15)
            if r["ok"]:
                parsed = _parse_router_output(r["output"])
                ips = [data["ip"] for iface, data in parsed.items() if data["ip"] != "unassigned"]
                if ips:
                    return name, ips[0]
                return name, "unassigned"
        return None, None

    # Fetch concurrently to save time
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_node = {executor.submit(fetch_ip_for_node, n): n for n in nodes}
        for future in concurrent.futures.as_completed(future_to_node):
            try:
                name, ip = future.result()
                if name and ip:
                    live_ips[name] = ip
            except Exception as e:
                _log("ERROR", f"Error fetching IP: {e}")

    _log("OK", f"Live IPs fetched: {live_ips}")
    return {"ok": True, "live_ips": live_ips}


# ─────────────────────────────────────────────────────────────────────────────
# LIVE TOPOLOGY IMPORT (reads the real GNS3 project: nodes, switches, links)
# ─────────────────────────────────────────────────────────────────────────────

def _node_kind(node_type: str) -> str:
    t = (node_type or "").lower()
    if t == "vpcs":
        return "pc"
    if "switch" in t or t == "ethernet_hub":
        return "switch"
    if t in ("cloud", "nat"):
        return "cloud"
    return "router"


def _port_short_name(node: dict, adapter: int, port: int) -> str:
    for p in node.get("ports", []):
        if p.get("adapter_number") == adapter and p.get("port_number") == port:
            return p.get("short_name") or p.get("name") or f"{adapter}/{port}"
    return f"{adapter}/{port}"


def _trailing_number(name: str):
    m = re.search(r"(\d+)\s*$", name or "")
    return int(m.group(1)) if m else None


def _natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name or "")]


def _plan_addressing(devices: list, connections: list, node_kind: dict) -> list:
    """
    Group device ports into layer-2 segments (switches/hubs are transparent) and
    give each segment a subnet. Fills interface IPs/gateways in `devices` in place.
    Returns the list of subnets used.
    """
    by_name = {d["id"]: d for d in devices}
    switches = [n for n, k in node_kind.items() if k == "switch"]
    parent = {s: s for s in switches}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for c in connections:
        a, b = c["from_device"], c["to_device"]
        if node_kind.get(a) == "switch" and node_kind.get(b) == "switch":
            parent[find(a)] = find(b)

    segments = {}  # key -> list of (device, iface)
    for i, c in enumerate(connections):
        a, b = c["from_device"], c["to_device"]
        ends = [(a, c["from_interface"]), (b, c["to_interface"])]
        sw = [e for e in ends if node_kind.get(e[0]) == "switch"]
        key = ("sw", find(sw[0][0])) if sw else ("p2p", i)
        for dev, iface in ends:
            if node_kind.get(dev) in ("router", "pc"):
                segments.setdefault(key, []).append((dev, iface))

    used_third = set()
    counter = {"lan": 100, "p2p": 100}
    subnets = []

    def pick(third, kind):
        if third is None or third > 255 or (kind, third) in used_third:
            third = counter[kind]
            while (kind, third) in used_third:
                third += 1
            counter[kind] = third + 1
        used_third.add((kind, third))
        return third

    def seg_order(kv):
        rs = [_natural_key(d) for d, _ in kv[1] if node_kind.get(d) == "router"]
        return min(rs) if rs else [chr(0x10ffff)]

    for key, members in sorted(segments.items(), key=seg_order):
        routers = sorted({d for d, _ in members if node_kind[d] == "router"}, key=_natural_key)
        pcs = sorted({d for d, _ in members if node_kind[d] == "pc"}, key=_natural_key)
        iface_of = {d: i for d, i in members}

        if len(routers) == 2 and not pcs:
            nums = [_trailing_number(r) for r in routers]
            third = int("".join(str(n) for n in nums)) if all(n is not None for n in nums) else None
            third = pick(third, "p2p")
            base, plen = f"10.0.{third}.", 30
            hosts = [(r, i + 1) for i, r in enumerate(routers)]
            gateway = None
        else:
            first = _trailing_number(routers[0]) if routers else None
            third = pick(first * 10 if first is not None else None, "lan")
            base, plen = f"192.168.{third}.", 24
            hosts = [(d, i + 1) for i, d in enumerate(routers + pcs)]
            gateway = f"{base}1" if routers else None

        for dev, host in hosts:
            for ifc in by_name[dev]["interfaces"]:
                if ifc["name"] == iface_of[dev]:
                    ifc["ip"] = f"{base}{host}/{plen}"
                    if node_kind[dev] == "pc" and gateway:
                        ifc["gateway"] = gateway
        subnets.append({"network": f"{base}0/{plen}",
                        "label": ("Link " if plen == 30 else "LAN ") + "/".join(routers or pcs)})
    return subnets


def get_project_topology(project_id: str) -> dict:
    """
    Read the real topology of a GNS3 project (devices incl. switches, links with port
    names, positions) and build the dashboard's .gns3-style topology plus an
    `_ai_topology` with a generated IP plan, so the website matches GNS3.
    """
    try:
        proj = _get(f"projects/{project_id}")
        raw_nodes = _get(f"projects/{project_id}/nodes")
        raw_links = _get(f"projects/{project_id}/links")
    except Exception as e:
        msg = f"Failed to read topology of project {project_id}: {e}"
        _log("ERROR", msg)
        return {"ok": False, "error": msg}

    by_id = {n["node_id"]: n for n in raw_nodes}
    node_kind = {n["name"]: _node_kind(n.get("node_type")) for n in raw_nodes}

    devices = []
    for n in raw_nodes:
        devices.append({
            "id": n["name"], "label": n["name"], "type": node_kind[n["name"]], "vendor": "cisco",
            "x": n.get("x", 0), "y": n.get("y", 0), "interfaces": [],
        })
    dev_by_name = {d["id"]: d for d in devices}

    topo_links, connections = [], []
    for lk in raw_links:
        ends = lk.get("nodes", [])
        if len(ends) != 2 or any(e["node_id"] not in by_id for e in ends):
            continue
        sides = []
        for e in ends:
            node = by_id[e["node_id"]]
            short = _port_short_name(node, e.get("adapter_number", 0), e.get("port_number", 0))
            if node_kind[node["name"]] == "pc":
                short = "eth0"
            sides.append((node["name"], short, e))
        topo_links.append({
            "link_id": lk["link_id"],
            "nodes": [{"node_id": e["node_id"], "adapter_number": e.get("adapter_number", 0),
                       "port_number": e.get("port_number", 0), "label": {"text": short}}
                      for _, short, e in sides],
        })
        connections.append({"from_device": sides[0][0], "from_interface": sides[0][1],
                            "to_device": sides[1][0], "to_interface": sides[1][1],
                            "link_type": "ethernet"})
        for name, short, _ in sides:
            if node_kind[name] in ("router", "pc"):
                dev_by_name[name]["interfaces"].append({"name": short, "ip": ""})

    subnets = _plan_addressing(devices, connections, node_kind)

    topo_nodes = [{
        "node_id": n["node_id"], "name": n["name"], "node_type": n.get("node_type", ""),
        "x": n.get("x", 0), "y": n.get("y", 0), "properties": {},
    } for n in raw_nodes]

    _log("OK", f"Imported topology of '{proj.get('name')}': {len(topo_nodes)} devices, "
               f"{len(topo_links)} links, {len(subnets)} subnets")
    return {
        "ok": True,
        "source": "gns3-live",
        "name": proj.get("name", ""),
        "topology": {"nodes": topo_nodes, "links": topo_links, "drawings": []},
        "_ai_topology": {"devices": devices, "connections": connections, "subnets": subnets},
    }


# ─────────────────────────────────────────────────────────────────────────────
# REAL PING TEST (VPCS consoles)
# ─────────────────────────────────────────────────────────────────────────────

def _vpcs_ping(host: str, port: int, targets: list, count: int = 3) -> dict:
    """Run `ping <ip> -c N` on a VPCS console for each target. Returns {ip: [rtt_ms, ...]}."""
    results = {}
    try:
        sock = socket.create_connection((host, port), timeout=8)
    except Exception as e:
        return {"error": f"console {host}:{port}: {e}"}

    def read(wait, until_lines=None):
        buf, end = b"", time.time() + wait
        while time.time() < end:
            try:
                sock.settimeout(0.25)
                chunk = sock.recv(4096)
                if not chunk:
                    break
                i, clean = 0, bytearray()
                while i < len(chunk):
                    if chunk[i] == 255 and i + 2 < len(chunk):
                        if chunk[i + 1] in (253, 251):
                            sock.sendall(bytes([255, 252 if chunk[i + 1] == 253 else 254, chunk[i + 2]]))
                        i += 3
                    else:
                        clean.append(chunk[i])
                        i += 1
                buf += bytes(clean)
                if until_lines and buf.decode("ascii", "replace").count("icmp_seq") >= until_lines:
                    time.sleep(0.3)
                    break
            except socket.timeout:
                pass
            except Exception:
                break
        return buf.decode("ascii", "replace")

    try:
        sock.sendall(b"\r\n")
        read(1.0)
        for ip in targets:
            _mark_web_ping(host, port, ip)
            sock.sendall(f"ping {ip} -c {count}\r\n".encode())
            out = read(count * 3 + 4, until_lines=count)
            results[ip] = [float(m) for m in
                           re.findall(r"bytes from \S+ icmp_seq=\d+ ttl=\d+ time=([\d.]+) ms", out)]
    finally:
        sock.close()
    return results


_WEB_PINGS = {}
_WEB_PINGS_LOCK = threading.Lock()


def _mark_web_ping(host, port, ip):
    with _WEB_PINGS_LOCK:
        _WEB_PINGS[(host, port, ip)] = time.time()


def _web_ping_recent(host, port, ip, within=180):
    with _WEB_PINGS_LOCK:
        t = _WEB_PINGS.get((host, port, ip))
    return bool(t and time.time() - t <= within)


def _ip_only(value):
    """Return a clean IPv4 string, or None if value is not an address (e.g. 'unassigned')."""
    try:
        return str(ipaddress.IPv4Address(str(value).split("/")[0].strip()))
    except ValueError:
        return None


def _sock_read(sock, wait, stop_pattern=None):
    """Read from a Telnet console for up to `wait` seconds (or until stop_pattern shows up)."""
    buf, end = b"", time.time() + wait
    while time.time() < end:
        try:
            sock.settimeout(0.25)
            chunk = sock.recv(4096)
            if not chunk:
                break
            i, clean = 0, bytearray()
            while i < len(chunk):
                if chunk[i] == 255 and i + 2 < len(chunk):
                    if chunk[i + 1] in (253, 251):
                        sock.sendall(bytes([255, 252 if chunk[i + 1] == 253 else 254, chunk[i + 2]]))
                    i += 3
                else:
                    clean.append(chunk[i])
                    i += 1
            buf += bytes(clean)
            if stop_pattern and re.search(stop_pattern, buf.decode("ascii", "replace")):
                time.sleep(0.3)
                break
        except socket.timeout:
            pass
        except Exception:
            break
    return buf.decode("ascii", "replace")


def _ios_ping(host: str, port: int, targets: list, count: int = 4) -> dict:
    """Run `ping <ip> repeat N` on an IOS router console. Returns {ip: {replies, sent, rtt}}."""
    try:
        sock = socket.create_connection((host, port), timeout=8)
    except Exception as e:
        return {"error": f"console {host}:{port}: {e}"}
    results = {}
    try:
        sock.sendall(b"\r\n")
        _sock_read(sock, 1.0)
        sock.sendall(b"end\r\n")
        _sock_read(sock, 0.8)
        for ip in targets:
            _mark_web_ping(host, port, ip)
            sock.sendall(f"ping {ip} repeat {count}\r\n".encode())
            out = _sock_read(sock, count * 2 + 10, stop_pattern=r"Success rate is[^\n]*\n")
            m = re.search(r"Success rate is \d+ percent \((\d+)/(\d+)\)"
                          r"(?:, round-trip min/avg/max = (\d+)/(\d+)/(\d+) ms)?", out)
            if m:
                rtt = [float(m.group(i)) for i in (3, 4, 5)] if m.group(3) else []
                results[ip] = {"replies": int(m.group(1)), "sent": int(m.group(2)), "rtt": rtt}
            else:
                results[ip] = {"replies": 0, "sent": count, "rtt": []}
    finally:
        sock.close()
    return results


def _read_pc_ip(host: str, port: int):
    """Actual IPv4 address configured on a VPCS node, or None."""
    try:
        out = _console_run(host, port, ["show ip"], gap=1.2, tail=0.8)
    except Exception:
        return None
    m = re.search(r"IP/MASK\s*:\s*(\d+\.\d+\.\d+\.\d+)", out)
    ip = _ip_only(m.group(1)) if m else None
    return ip if ip and ip != "0.0.0.0" else None


def _l3_inventory(project_id: str) -> dict:
    """Routers and PCs of a project with their consoles and addresses (planned + actual)."""
    topo = get_project_topology(project_id)
    if not topo.get("ok"):
        return topo
    nodes = get_project_nodes(project_id)
    if not nodes.get("ok"):
        return nodes
    console_of = {n["name"]: (n.get("console_host", "127.0.0.1"), n.get("console"))
                  for n in nodes["nodes"] if n.get("status") == "started"}
    devs = [d for d in topo["_ai_topology"]["devices"] if d["type"] in ("router", "pc")]
    plan = {d["id"]: [i["ip"] for i in d["interfaces"] if _ip_only(i.get("ip"))] for d in devs}
    kind = {d["id"]: d["type"] for d in devs}

    actual, lock = {}, threading.Lock()

    def read_pc(name):
        host, port = console_of.get(name, (None, None))
        if port:
            ip = _read_pc_ip(host, port)
            if ip:
                with lock:
                    actual[name] = ip

    threads = [threading.Thread(target=read_pc, args=(n,)) for n, k in kind.items() if k == "pc"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return {"ok": True, "console_of": console_of, "plan": plan, "kind": kind, "actual": actual,
            "names": sorted(kind, key=lambda n: (kind[n] != "router", _natural_key(n)))}


def _pick_dest_ip(inv: dict, src: str, dst: str):
    """Address of `dst` to ping from `src`: the one on a subnet they share, else its first one."""
    if inv["kind"].get(dst) == "pc" and dst in inv["actual"]:
        return inv["actual"][dst]
    src_nets = set()
    for c in inv["plan"].get(src, []):
        try:
            src_nets.add(ipaddress.IPv4Interface(c).network)
        except ValueError:
            pass
    cands = inv["plan"].get(dst, [])
    for c in cands:
        try:
            if ipaddress.IPv4Interface(c).network in src_nets:
                return _ip_only(c)
        except ValueError:
            pass
    return _ip_only(cands[0]) if cands else None


def run_ping_matrix(project_id: str, count: int = 3) -> dict:
    """Every router and PC pings every other router and PC, on their own consoles."""
    inv = _l3_inventory(project_id)
    if not inv.get("ok"):
        return inv
    names, kind, console_of = inv["names"], inv["kind"], inv["console_of"]
    out, lock = [], threading.Lock()

    def worker(src):
        host, port = console_of.get(src, (None, None))
        pairs = [(d, _pick_dest_ip(inv, src, d)) for d in names if d != src]
        ips = [ip for _, ip in pairs if ip]
        if not port:
            res = {"error": "no console"}
        elif kind[src] == "pc":
            res = _vpcs_ping(host, port, ips, count)
        else:
            res = _ios_ping(host, port, ips, count + 1)
        for dst, ip in pairs:
            replies, sent, rtts, triple = 0, count, [], False
            if ip and "error" not in res:
                r = res.get(ip)
                if isinstance(r, dict):          # router: IOS reports min/avg/max itself
                    replies, sent, rtts = r["replies"], r["sent"], r["rtt"]
                    triple = bool(rtts)
                elif isinstance(r, list):
                    replies, rtts = len(r), r
            if triple:
                lo, av, hi = rtts
            else:
                lo = min(rtts) if rtts else None
                av = sum(rtts) / len(rtts) if rtts else None
                hi = max(rtts) if rtts else None
            row = {"source": src, "dest": dst, "dest_ip": ip or "no IP", "replies": replies, "sent": sent,
                   "status": "SUCCESS" if replies else "FAIL",
                   "min": round(lo, 1) if lo is not None else None,
                   "avg": round(av, 1) if av is not None else None,
                   "max": round(hi, 1) if hi is not None else None}
            with lock:
                out.append(row)

    threads = [threading.Thread(target=worker, args=(s,)) for s in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    order = {n: i for i, n in enumerate(names)}
    out.sort(key=lambda r: (order[r["source"]], order[r["dest"]]))
    ok = sum(r["status"] == "SUCCESS" for r in out)
    _log("OK" if ok == len(out) else "WARN", f"Ping test: {ok}/{len(out)} paths reachable")
    device_ips = {n: ([inv["actual"][n]] if n in inv["actual"] else inv["plan"].get(n, [])) for n in names}
    return {"ok": True, "results": out, "device_ips": device_ips}


def run_single_ping(project_id: str, source: str, target: str, count: int = 4) -> dict:
    """Ping from one device to an IP or another device and return the console output."""
    inv = _l3_inventory(project_id)
    if not inv.get("ok"):
        return inv
    names = inv["names"]
    src = next((n for n in names if n.lower() == source.strip().lower()), None)
    if not src:
        return {"ok": False, "error": f"'{source}' is not a router or PC in this project."}
    host, port = inv["console_of"].get(src, (None, None))
    if not port:
        return {"ok": False, "error": f"{src} is not running (no console). Start it in GNS3 first."}
    ip = _ip_only(target)
    label = ip
    if not ip:
        dst = next((n for n in names if n.lower() == target.strip().lower()), None)
        if not dst:
            return {"ok": False, "error": f"'{target}' is neither an IP address nor a known router/PC."}
        ip = _pick_dest_ip(inv, src, dst)
        label = f"{dst} ({ip})"
        if not ip:
            return {"ok": False, "error": f"{dst} has no IP address yet."}

    _mark_web_ping(host, port, ip)
    if inv["kind"][src] == "pc":
        cmd, gap = f"ping {ip} -c {count}", count * 3 + 3
        raw = _console_run(host, port, [cmd], gap=gap, tail=0.5, anchor=cmd, stop=rf"icmp_seq={count}\b")
        replies = len(re.findall(r"bytes from", raw))
    else:
        cmd, gap = f"ping {ip} repeat {count}", count * 2 + 10
        raw = _console_run(host, port, ["end", cmd], gap=gap, tail=0.5, anchor=cmd,
                           stop=r"Success rate is[^\n]*\n")
        m = re.search(r"Success rate is \d+ percent \((\d+)/(\d+)\)", raw)
        replies = int(m.group(1)) if m else 0
    _log("OK" if replies else "WARN", f"Manual ping {src} -> {label}: {replies}/{count} replies")
    return {"ok": True, "source": src, "target": label, "target_ip": ip, "output": raw,
            "replies": replies, "sent": count, "status": "SUCCESS" if replies else "FAIL"}


# ─────────────────────────────────────────────────────────────────────────────
# PINGS TYPED IN THE GNS3 CONSOLES (read back from each device's console history)
# ─────────────────────────────────────────────────────────────────────────────

_PROMPT_LINE = re.compile(r"^[A-Za-z0-9_\-\.]+(?:\([A-Za-z0-9\-]+\))?[#>]")


def _console_history(host: str, port: int, wait: float = 3.0) -> str:
    """Everything the device console replays when a new Telnet client connects."""
    sock = socket.create_connection((host, port), timeout=8)
    try:
        sock.sendall(b"\r\n")
        return _sock_read(sock, wait).replace("\r", "")
    finally:
        sock.close()


def _parse_ping_history(text: str) -> list:
    """Find `ping <ip>` commands and their results in console text (IOS router or VPCS)."""
    lines = text.split("\n")
    found, i = [], 0
    while i < len(lines):
        m = re.search(r"[#>]\s*ping\s+(\d+\.\d+\.\d+\.\d+)", lines[i])
        if not m:
            i += 1
            continue
        ip, replies, sent, rtts, triple, j = m.group(1), 0, 0, [], False, i + 1
        while j < len(lines) and j <= i + 60 and not _PROMPT_LINE.match(lines[j].strip()):
            ln = lines[j]
            sr = re.search(r"Success rate is \d+ percent \((\d+)/(\d+)\)"
                           r"(?:, round-trip min/avg/max = (\d+)/(\d+)/(\d+) ms)?", ln)
            if sr:  # IOS router
                replies, sent = int(sr.group(1)), int(sr.group(2))
                rtts = [float(sr.group(k)) for k in (3, 4, 5)] if sr.group(3) else []
                triple = bool(rtts)
                break
            if "icmp_seq=" in ln:  # VPCS
                sent += 1
                t = re.search(r"time=([\d.]+) ms", ln)
                if "bytes from" in ln and t:
                    replies += 1
                    rtts.append(float(t.group(1)))
            j += 1
        if sent:
            found.append({"ip": ip, "replies": replies, "sent": sent, "rtts": rtts, "triple": triple})
        i = max(j, i + 1)
    return found


def read_console_pings(project_id: str) -> dict:
    """
    Ping history of the GNS3 consoles: pings typed by hand in GNS3 (routers and PCs) and pings
    run by the website. Live pings are captured by console_watch; each router's replayed console
    history adds what happened before the watcher started.
    """
    topo = get_project_topology(project_id)
    if not topo.get("ok"):
        return topo
    nodes = get_project_nodes(project_id)
    if not nodes.get("ok"):
        return nodes
    console_of = {n["name"]: (n.get("console_host", "127.0.0.1"), n.get("console"))
                  for n in nodes["nodes"] if n.get("status") == "started" and n.get("console")}
    devs = [d for d in topo["_ai_topology"]["devices"] if d["type"] in ("router", "pc") and d["id"] in console_of]
    ip_owner = {}
    for d in topo["_ai_topology"]["devices"]:
        for i in d["interfaces"]:
            ip = _ip_only(i.get("ip"))
            if ip:
                ip_owner[ip] = d["id"]

    import console_watch
    fresh = console_watch.ensure_watchers([(d["id"], console_of[d["id"]][0], console_of[d["id"]][1]) for d in devs])
    if fresh:
        time.sleep(3.5)  # new watchers need a moment to connect and read the replayed history
    live, replayed = console_watch.snapshot([d["id"] for d in devs])

    def row(src, r, when, origin):
        rtts = r["rtts"]
        if r.get("triple"):                 # IOS already reports min/avg/max
            lo, av, hi = rtts
        else:
            lo = min(rtts) if rtts else None
            av = sum(rtts) / len(rtts) if rtts else None
            hi = max(rtts) if rtts else None
        return {"time": when, "source": src, "dest_ip": r["ip"], "dest": ip_owner.get(r["ip"], ""),
                "replies": r["replies"], "sent": r["sent"], "origin": origin,
                "status": "SUCCESS" if r["replies"] else "FAIL",
                "min": round(lo, 1) if lo is not None else None,
                "avg": round(av, 1) if av is not None else None,
                "max": round(hi, 1) if hi is not None else None}

    rows = [row(e["source"], e, e["time"], e["origin"]) for e in live[:40]]
    earlier = []
    for src, per_ip in replayed.items():
        for ip, r in per_ip.items():
            earlier.append(row(src, r, "earlier", "Console history"))
    earlier.sort(key=lambda r: (_natural_key(r["source"]), _natural_key(r["dest_ip"])))
    return {"ok": True, "results": rows + earlier, "live": len(rows)}

# ─────────────────────────────────────────────────────────────────────────────
# AUTOMATION REPORT (PRD FR-6)
# ─────────────────────────────────────────────────────────────────────────────

_RESULTS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
)


def _console_run(host: str, port: int, cmds: list, gap: float = 1.0, tail: float = 1.5,
                 anchor: str = None, stop: str = None) -> str:
    """Send commands to a device console and return what they printed (without old scrollback)."""
    sock = socket.create_connection((host, port), timeout=8)

    def read(wait):
        buf, end = b"", time.time() + wait
        while time.time() < end:
            try:
                sock.settimeout(0.2)
                chunk = sock.recv(4096)
                if not chunk:
                    break
                i, clean = 0, bytearray()
                while i < len(chunk):
                    if chunk[i] == 255 and i + 2 < len(chunk):
                        if chunk[i + 1] in (253, 251):
                            sock.sendall(bytes([255, 252 if chunk[i + 1] == 253 else 254, chunk[i + 2]]))
                        i += 3
                    else:
                        clean.append(chunk[i])
                        i += 1
                buf += bytes(clean)
            except socket.timeout:
                pass
            except Exception:
                break
        return buf.decode("ascii", "replace")

    out = ""
    try:
        sock.sendall(b"\r\n")
        out += read(1.0)
        for idx, c in enumerate(cmds):
            sock.sendall((c + "\r\n").encode())
            last = idx == len(cmds) - 1
            if stop and last:
                out += _sock_read(sock, gap, stop_pattern=stop)   # return as soon as the command is done
            elif stop:
                out += read(1.0)
            else:
                out += read(gap)
        out += read(tail)
    finally:
        sock.close()
    out = out.replace("\r", "")
    # The console replays its history when we connect; the anchor command is the last one echoed.
    cut = out.rfind(anchor or cmds[0])
    if cut >= 0:
        out = out[cut:]
    return "\n".join(l.rstrip() for l in out.splitlines() if l.strip())


def build_report(project_id: str) -> dict:
    """
    Collect topology, addressing, live device state and real ping results into a
    text report, save it under results/, and return it.
    """
    topo = get_project_topology(project_id)
    if not topo.get("ok"):
        return topo
    nodes_res = get_project_nodes(project_id)
    if not nodes_res.get("ok"):
        return nodes_res
    nodes = {n["name"]: n for n in nodes_res["nodes"]}
    ai = topo["_ai_topology"]
    devices = ai["devices"]
    try:
        version = _get("version").get("version", "?")
    except Exception:
        version = "?"

    # Live state of every device (in parallel)
    state, lock = {}, threading.Lock()

    def collect(dev):
        n = nodes.get(dev["id"])
        if not n or not n.get("console") or n.get("status") != "started":
            return
        try:
            if dev["type"] == "router":
                cmds = ["terminal length 0", "show ip interface brief", "show ip ospf neighbor", "show ip route"]
                text = _console_run(n["console_host"], n["console"], cmds, gap=1.3)
            elif dev["type"] == "pc":
                text = _console_run(n["console_host"], n["console"], ["show ip"], gap=1.0)
            else:
                return
        except Exception as e:
            text = f"(could not read console: {e})"
        with lock:
            state[dev["id"]] = text

    threads = [threading.Thread(target=collect, args=(d,)) for d in devices]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ping = run_ping_matrix(project_id)
    pings = ping.get("results", []) if ping.get("ok") else []

    n_r = sum(d["type"] == "router" for d in devices)
    n_s = sum(d["type"] == "switch" for d in devices)
    n_p = sum(d["type"] == "pc" for d in devices)
    ospf_full = sum(len(re.findall(r"\bFULL/", t)) for d, t in
                    ((dev, state.get(dev["id"], "")) for dev in devices if dev["type"] == "router"))
    ping_ok = sum(p["status"] == "SUCCESS" for p in pings)
    ifaces_up = sum(len(re.findall(r"\bup\s+up\b", state.get(d["id"], ""))) for d in devices if d["type"] == "router")

    line = "=" * 74
    sub = "-" * 74
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    out = [line, " AI-BASED AUTOMATIC NETWORK CONFIGURATION SYSTEM - AUTOMATION REPORT", line,
           f"Generated    : {now}",
           f"GNS3 project : {topo.get('name')}   (GNS3 server v{version})",
           f"Devices      : {len(devices)}  ({n_r} routers, {n_s} switches, {n_p} PCs)",
           f"Links        : {len(ai['connections'])}",
           f"Subnets      : {len(ai['subnets'])}", "",
           "1. TOPOLOGY (links read from GNS3)", sub]
    for c in ai["connections"]:
        out.append(f"   {c['from_device']:<9}{c['from_interface']:<7} <---->  {c['to_device']:<9}{c['to_interface']}")
    out += ["", "2. ADDRESSING PLAN", sub]
    for s in ai["subnets"]:
        out.append(f"   {s['network']:<18} {s['label']}")
    out.append("")
    for d in devices:
        for i in d["interfaces"]:
            gw = f"  gateway {i['gateway']}" if i.get("gateway") else ""
            out.append(f"   {d['id']:<9}{i['name']:<7} {i.get('ip', ''):<18}{gw}")
    out += ["", "3. DEVICE VERIFICATION (live output from the devices)", sub]
    for d in devices:
        if d["type"] == "switch":
            out += [f"[{d['id']}] unmanaged GNS3 switch - no configuration required", ""]
        elif d["id"] in state:
            out += [f"[{d['id']}]", state[d["id"]], ""]
        else:
            out += [f"[{d['id']}] not running - no data", ""]
    out += ["4. CONNECTIVITY (real ping tests run from each PC)", sub,
            f"   {'Source':<8}{'Destination':<24}{'Result':<9}{'Replies':<9}Min/Avg/Max (ms)"]
    for p in pings:
        rtt = f"{p['min']}/{p['avg']}/{p['max']}" if p["status"] == "SUCCESS" else "-"
        out.append(f"   {p['source']:<8}{p['dest_ip'] + ' (' + p['dest'] + ')':<24}{p['status']:<9}"
                   f"{str(p['replies']) + '/' + str(p['sent']):<9}{rtt}")
    out += ["", "5. SUMMARY", sub,
            f"   Router interfaces up/up      : {ifaces_up}",
            f"   OSPF adjacencies in FULL     : {ospf_full}",
            f"   Ping paths reachable         : {ping_ok}/{len(pings)}",
            f"   OVERALL RESULT               : "
            + ("PASS" if pings and ping_ok == len(pings) else "CHECK"), "", line]
    text = "\n".join(out) + "\n"

    os.makedirs(_RESULTS_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", topo.get("name") or "project")
    filename = f"network_report_{safe}_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    with open(os.path.join(_RESULTS_DIR, filename), "w", encoding="utf-8") as fh:
        fh.write(text)
    _log("OK", f"Report saved: results/{filename}")
    return {"ok": True, "filename": filename, "report": text,
            "summary": {"ping_ok": ping_ok, "ping_total": len(pings), "ospf_full": ospf_full}}
