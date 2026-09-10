"""Prompt construction for the discovery loop.

The model is given a job with hard edges: it sees one screen at a time, it picks
one action at a time, and it addresses controls by the reference numbers printed
on both the inventory and the screenshot. It is never given a URL to construct,
a selector to write, or the ability to run code.

Two things in here are load-bearing beyond "make the model work":

* the model is told to use ``extract`` for every value the capability must
  return, rather than reporting values in prose. That is what produces a
  *locator* for each output instead of a number the compiler would have to guess
  the provenance of;
* the model is told which named parameters it is filling in and with which
  sample values, so the compiler can parameterize the recording exactly rather
  than pattern-matching literals afterwards.
"""

from __future__ import annotations

from ..perception.annotate import inventory_text
from ..surfaces.model import Observation

SYSTEM = """\
You are the discovery half of an automation system for back-office banking
software. You drive a real application the way a human teller does: you look at
one screen, choose ONE action, and look again.

Perception
- You receive a numbered inventory of on-screen controls and an annotated
  screenshot with the same numbers drawn on it. Refer to controls ONLY by their
  reference (e.g. "e7"). References are valid for the current screen only.
- Accessible names are often EMPTY on this application. Identify a field by the
  label text sitting next to it, exactly as a person would.

Actions -- reply with exactly one, as JSON:
  {"kind":"click","target_ref":"e7"}
  {"kind":"type","target_ref":"e7","text":"..."}
  {"kind":"select","target_ref":"e7","text":"<option text>"}
  {"kind":"press","keys":"enter"}
  {"kind":"wait","ms":1500}
  {"kind":"extract","target_ref":"e7","bind":"<output_name>"}
  {"kind":"finish","outputs":{...}}
  {"kind":"escalate","reason":"..."}

Rules
1. Reply with ONE JSON object and nothing else:
   {"thought":"<one sentence: what you see and why this action>","action":{...}}
2. Every value the goal asks you to return MUST be captured with an `extract`
   action naming the exact control that displays it. Never retype a value you
   read; never put a value into `finish` that you did not `extract`.
3. `finish` only once the goal is visibly satisfied on screen. `finish` outputs
   should list the output names you bound with `extract`.
4. `escalate` if you are blocked, if the screen asks for something you were not
   given, or if the only way forward looks irreversible (posting a transaction,
   deleting, confirming a transfer). Escalating is a correct answer, not a
   failure.
5. Do not invent member numbers, amounts or credentials. Use only the values
   supplied to you.
6. Prefer the smallest step. Do not chain assumptions about screens you have not
   seen yet.
"""


def render_observation(obs: Observation, *, max_text_chars: int = 1400) -> str:
    text = obs.text_digest[:max_text_chars]
    if len(obs.text_digest) > max_text_chars:
        text += " ...[truncated]"
    return (
        f"SCREEN TITLE : {obs.title}\n"
        f"ROUTE        : {obs.route}\n"
        f"SIGNATURE    : {obs.signature()}\n\n"
        f"CONTROL INVENTORY\n{inventory_text(obs.elements)}\n\n"
        f"VISIBLE TEXT\n{text}\n"
    )


def render_history(history: list[str], *, keep: int = 12) -> str:
    if not history:
        return "(nothing yet -- this is the first step)"
    shown = history[-keep:]
    prefix = "" if len(history) <= keep else f"... {len(history) - keep} earlier steps omitted\n"
    return prefix + "\n".join(shown)


def build_user_prompt(
    *,
    goal: str,
    obs: Observation,
    history: list[str],
    parameters: dict[str, str],
    wanted_outputs: list[str],
    steps_left: int,
    last_error: str | None = None,
) -> str:
    param_block = (
        "\n".join(f"  {name} = {value!r}" for name, value in parameters.items())
        or "  (none -- this capability takes no inputs)"
    )
    output_block = (
        "\n".join(f"  {name}" for name in wanted_outputs)
        or "  (not specified -- choose sensible snake_case names)"
    )
    error_block = f"\nLAST ACTION FAILED: {last_error}\nChoose a different approach.\n" if last_error else ""

    return f"""\
GOAL
{goal}

INPUT PARAMETERS you may use (use these literal values, nothing else):
{param_block}

OUTPUTS the caller expects you to `extract`:
{output_block}

STEPS TAKEN SO FAR
{render_history(history)}
{error_block}
CURRENT SCREEN
{render_observation(obs)}

You have {steps_left} step(s) remaining. Reply with one JSON object.
"""


#: Type 2 (hybrid) replay consults the model for interpretation only. It is
#: given a closed list of outcome codes and cannot propose an action.
CLASSIFY_SYSTEM = """\
You classify one screen of a back-office banking application into exactly one of
a fixed list of outcome codes. You do not choose actions and you do not control
anything. Reply with one JSON object:
  {"thought":"<one sentence>","action":{"kind":"classify","code":"<CODE>","confidence":0.0-1.0}}
Use UNKNOWN if no code fits. Never invent a code that is not in the list.
"""


def build_classify_prompt(obs: Observation, codes: list[tuple[str, str]], step_intent: str) -> str:
    listing = "\n".join(f"  {code}: {desc}" for code, desc in codes)
    return f"""\
The automation was executing the step: {step_intent}
It reached a screen it does not recognize and must decide what happened.

PERMITTED OUTCOME CODES
{listing}
  UNKNOWN: none of the above

CURRENT SCREEN
{render_observation(obs, max_text_chars=1200)}

Which code describes this screen?
"""
