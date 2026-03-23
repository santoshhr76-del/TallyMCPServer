"""
Display utilities for the Web Data Processing Pipeline.

Handles pretty-printing of SDK messages, progress banners, and pipeline steps.
"""

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
)


# ANSI colour codes for terminal output
RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
DIM    = "\033[2m"
BLUE   = "\033[94m"
MAGENTA = "\033[95m"


def print_banner(title: str) -> None:
    """Print a formatted banner for the pipeline start."""
    width = 60
    print(f"\n{CYAN}{BOLD}{'═' * width}{RESET}")
    print(f"{CYAN}{BOLD}  🔗  {title.center(width - 6)}{RESET}")
    print(f"{CYAN}{BOLD}{'═' * width}{RESET}\n")


def print_pipeline_step(step: str, description: str = "") -> None:
    """Print a formatted pipeline step header."""
    print(f"\n{BLUE}{BOLD}▶ {step}{RESET}")
    if description:
        print(f"  {DIM}{description}{RESET}")
    print(f"  {DIM}{'─' * 50}{RESET}")


def print_message(message, verbose: bool = False) -> None:
    """
    Pretty-print a single SDK message.

    Args:
        message:  Any message type from claude_agent_sdk.
        verbose:  If True, also print tool call details; otherwise only
                  show Claude's text output and final results.
    """
    # ── System / init messages ────────────────────────────────────────────
    if isinstance(message, SystemMessage):
        if verbose:
            session_id = getattr(message, "session_id", "unknown")
            print(f"  {DIM}[Session: {session_id}]{RESET}")
        return

    # ── Assistant messages (reasoning + tool calls) ───────────────────────
    if isinstance(message, AssistantMessage):
        for block in message.content:
            # Text block — Claude's reasoning / narration
            if hasattr(block, "text") and block.text.strip():
                print(f"  {GREEN}{block.text.strip()}{RESET}")

            # Tool use block
            elif hasattr(block, "name") and verbose:
                tool_name = block.name
                tool_input = getattr(block, "input", {})

                # Summarise large inputs instead of dumping them
                if tool_name in ("Write", "Edit") and "content" in tool_input:
                    preview = str(tool_input.get("content", ""))[:80]
                    print(
                        f"  {YELLOW}[Tool] {tool_name} → "
                        f"{tool_input.get('file_path', '')} "
                        f"({len(str(tool_input.get('content','')))} chars){RESET}"
                    )
                elif tool_name == "Agent":
                    agent_name = tool_input.get("agent_name", "unknown")
                    print(f"\n  {MAGENTA}{BOLD}  ┌─ Spawning subagent: {agent_name} ─┐{RESET}")
                else:
                    # Generic tool display
                    summary = ", ".join(
                        f"{k}={str(v)[:40]}" for k, v in tool_input.items()
                    )
                    print(f"  {YELLOW}[Tool] {tool_name}({summary}){RESET}")
        return

    # ── Result message (pipeline complete) ───────────────────────────────
    if isinstance(message, ResultMessage):
        subtype = getattr(message, "subtype", "unknown")
        result_text = getattr(message, "result", "")
        cost_usd = getattr(message, "cost_usd", None)
        turns = getattr(message, "num_turns", None)

        icon = "✅" if subtype == "success" else "❌"
        colour = GREEN if subtype == "success" else RED

        print(f"\n{colour}{BOLD}{'─' * 60}{RESET}")
        print(f"{colour}{BOLD}{icon} Pipeline {subtype.upper()}{RESET}")
        if result_text:
            print(f"{colour}{result_text[:400]}{RESET}")
        if cost_usd is not None:
            print(f"  {DIM}Cost: ${cost_usd:.4f} | Turns: {turns}{RESET}")
        print(f"{colour}{BOLD}{'─' * 60}{RESET}\n")
        return

    # ── Fallback for unknown message types ────────────────────────────────
    if verbose:
        print(f"  {DIM}[{type(message).__name__}]{RESET}")


def print_error(message: str) -> None:
    """Print a formatted error message."""
    print(f"\n{RED}{BOLD}✗ ERROR: {message}{RESET}\n")


def print_success(message: str) -> None:
    """Print a formatted success message."""
    print(f"\n{GREEN}{BOLD}✓ {message}{RESET}\n")
