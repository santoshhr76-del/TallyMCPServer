"""
Display utilities — pretty-print Claude Agent SDK messages with colour.
"""

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage

RESET   = "\033[0m"
BOLD    = "\033[1m"
CYAN    = "\033[96m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
DIM     = "\033[2m"
BLUE    = "\033[94m"
MAGENTA = "\033[95m"


def print_banner(title: str) -> None:
    width = 62
    print(f"\n{CYAN}{BOLD}{'═' * width}{RESET}")
    print(f"{CYAN}{BOLD}  📊  {title.center(width - 6)}{RESET}")
    print(f"{CYAN}{BOLD}{'═' * width}{RESET}\n")


def print_pipeline_step(step: str, description: str = "") -> None:
    print(f"\n{BLUE}{BOLD}▶ {step}{RESET}")
    if description:
        print(f"  {DIM}{description}{RESET}")
    print(f"  {DIM}{'─' * 52}{RESET}")


def print_message(message, verbose: bool = False) -> None:
    if isinstance(message, SystemMessage):
        if verbose:
            print(f"  {DIM}[Session: {getattr(message, 'session_id', '?')}]{RESET}")
        return

    if isinstance(message, AssistantMessage):
        for block in message.content:
            if hasattr(block, "text") and block.text.strip():
                print(f"  {GREEN}{block.text.strip()}{RESET}")
            elif hasattr(block, "name") and verbose:
                name = block.name
                inp  = getattr(block, "input", {})
                if name == "Agent":
                    agent_name = inp.get("agent_name", "unknown")
                    print(f"\n  {MAGENTA}{BOLD}  ╔═ Spawning: {agent_name} ═╗{RESET}")
                elif name in ("Write", "Edit"):
                    fp = inp.get("file_path", "")
                    print(f"  {YELLOW}[Tool] {name} → {fp}{RESET}")
                else:
                    summary = ", ".join(f"{k}={str(v)[:40]}" for k, v in inp.items())
                    print(f"  {YELLOW}[Tool] {name}({summary}){RESET}")
        return

    if isinstance(message, ResultMessage):
        subtype = getattr(message, "subtype", "unknown")
        result  = getattr(message, "result", "")
        cost    = getattr(message, "cost_usd", None)
        turns   = getattr(message, "num_turns", None)
        icon    = "✅" if subtype == "success" else "❌"
        colour  = GREEN if subtype == "success" else RED
        print(f"\n{colour}{BOLD}{'─' * 62}{RESET}")
        print(f"{colour}{BOLD}{icon}  Pipeline {subtype.upper()}{RESET}")
        if result:
            print(f"{colour}{result[:500]}{RESET}")
        if cost is not None:
            print(f"  {DIM}Cost: ${cost:.4f}  |  Turns: {turns}{RESET}")
        print(f"{colour}{BOLD}{'─' * 62}{RESET}\n")
        return

    if verbose:
        print(f"  {DIM}[{type(message).__name__}]{RESET}")


def print_error(msg: str) -> None:
    print(f"\n{RED}{BOLD}✗ ERROR: {msg}{RESET}\n")


def print_success(msg: str) -> None:
    print(f"\n{GREEN}{BOLD}✓ {msg}{RESET}\n")
