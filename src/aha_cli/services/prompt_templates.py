from __future__ import annotations

from importlib import resources
from string import Template


def render_prompt_template(name: str, **values: object) -> str:
    template = resources.files("aha_cli.prompts").joinpath(name).read_text(encoding="utf-8")
    text_values = {key: "" if value is None else str(value) for key, value in values.items()}
    rendered = Template(template).substitute(text_values).strip()
    return rendered + "\n"
