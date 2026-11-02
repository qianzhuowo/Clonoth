"""Pure preparation helpers for the opt-in NapCat WS boundary deployment.

No import-time effects, subprocess execution, token generation or automatic deployment.
The caller must arm independent recovery and back up live data before applying changes.
Secrets returned by these helpers must remain server-side in root-only files.
"""
from __future__ import annotations

import copy
import ipaddress
import re

NETWORK = "clonoth-napcat-secure"
BRIDGE = "nc-ws0"
SUBNET = "172.30.83.0/29"
GATEWAY = "172.30.83.1"
CLIENT = "172.30.83.2"
PORT = 8080
COMMENT = "clonoth-napcat-ws-boundary-v1"
URL = f"ws://{GATEWAY}:{PORT}/onebot/v11/ws"
OLD_URL = "ws://172.17.0.1:8080/onebot/v11/ws"
TOKEN_KEYS = {"ONEBOT_ACCESS_TOKEN", "ONEBOT_V11_ACCESS_TOKEN"}


def validate_token(token: str) -> None:
    # Only accepts the planned server-generated 256-bit hexadecimal token.
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("Expected a server-generated 256-bit hexadecimal token")


def patch_env(text: str, token: str) -> str:
    """Preserve unrelated settings; replace bind and both possible token aliases."""
    validate_token(token)
    lines = []
    seen = set()
    for line in text.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match:
            key = match.group(1).upper()
            if key in {"HOST", "PORT"} | TOKEN_KEYS:
                if key in seen:
                    raise ValueError("Duplicate managed environment setting")
                seen.add(key)
                continue
        lines.append(line)
    if "HOST" not in seen or "PORT" not in seen:
        raise ValueError("Expected explicit original HOST and PORT")
    # Set both aliases identically to avoid precedence surprises across adapter versions.
    lines.extend([f"HOST={GATEWAY}", f"PORT={PORT}",
                  f"ONEBOT_ACCESS_TOKEN={token}", f"ONEBOT_V11_ACCESS_TOKEN={token}"])
    return "\n".join(lines) + "\n"


def patch_onebot(config: dict, token: str) -> dict:
    """Update exactly one known reverse client, rejecting unexpected active listeners."""
    validate_token(token)
    result = copy.deepcopy(config)
    network = result.get("network")
    if not isinstance(network, dict):
        raise ValueError("Missing OneBot network configuration")
    for kind in ("websocketServers", "httpServers", "httpSseServers", "httpClients"):
        if any(entry.get("enable") for entry in network.get(kind, [])):
            raise ValueError("Unexpected active OneBot endpoint")
    clients = network.get("websocketClients", [])
    active = [entry for entry in clients if entry.get("enable")]
    if len(active) != 1:
        raise ValueError("Expected exactly one enabled reverse client")
    client = active[0]
    if client.get("name") != "clonoth" or client.get("url") not in {OLD_URL, URL}:
        raise ValueError("Unexpected reverse client identity or URL")
    client.update(url=URL, token=token)
    return result


def check_subnet_conflicts(subnets: list[str]) -> None:
    candidate = ipaddress.ip_network(SUBNET)
    for text in subnets:
        net = ipaddress.ip_network(text, strict=False)
        # Default route is not an allocated subnet.
        if net.version == 4 and net.prefixlen and candidate.overlaps(net):
            raise ValueError("Dedicated bridge subnet overlaps an existing network/route")


def input_rules() -> list[list[str]]:
    """Ordered terminal rules scoped to a single host address and TCP port.

    These are INPUT rules, not DOCKER-USER rules: the receiver is a host process.
    Place them ahead of broad UFW/established accepts. Do not flush any chain.
    The host loopback exception supports local negative-auth checks. Local root
    and administrators able to change Docker networks/firewalls remain trusted.
    """
    scope = ["-d", f"{GATEWAY}/32", "-p", "tcp", "--dport", str(PORT),
             "-m", "comment", "--comment", COMMENT]
    return [
        [*scope, "-i", "lo", "-j", "ACCEPT"],
        [*scope, "-i", BRIDGE, "-s", f"{CLIENT}/32", "-j", "ACCEPT"],
        [*scope, "-j", "REJECT", "--reject-with", "tcp-reset"],
    ]


def rule_commands(action: str) -> list[list[str]]:
    """Return, but never execute, exact reversible rule commands.

    Caller must check `iptables -C INPUT <rule>` before inserting/deleting and
    journal each action. Top insertion is reversed to preserve allow-before-deny.
    Never use restore/flush or delete rules by unstable numeric indices.
    """
    if action == "apply":
        return [["iptables", "-w", "10", "-I", "INPUT", "1", *rule]
                for rule in reversed(input_rules())]
    if action == "remove":
        return [["iptables", "-w", "10", "-D", "INPUT", *rule]
                for rule in input_rules()]
    raise ValueError("Unknown firewall action")
