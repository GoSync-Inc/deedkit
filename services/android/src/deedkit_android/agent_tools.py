"""The arguments of the local agent's eleven tools, as the service validates them (ANYM-971 criterion 3). phone_mcp.py
lists the same fields to the engine; a test keeps the two together."""

import re
from typing import Literal

from pydantic import Field, model_validator

from .config import Package, Refused, Strict
from .phone_mcp import TOOLS
from .secrets import REFERENCE

POINT = re.compile(r'\s*\d+(?:\.\d+)?%?\s*,\s*\d+(?:\.\d+)?%?\s*')


class Selector(Strict):
    text: str | None = None
    textExact: str | None = None
    id: str | None = None
    resourceId: str | None = None
    description: str | None = None
    className: str | None = None
    index: int | None = Field(default=None, ge=0)
    enabled: bool | None = None
    selected: bool | None = None
    checked: bool | None = None
    focused: bool | None = None
    clickable: bool | None = None
    scrollable: bool | None = None

    @model_validator(mode='after')
    def not_empty(self):
        if not self.model_fields_set:
            raise ValueError('empty_selector')
        return self

    def fields(self):
        found = self.model_dump(exclude_none=True)
        if not found:
            raise Refused('invalid_arguments', 422)
        return found


def point(value):
    if value is not None and not POINT.fullmatch(value):
        raise ValueError('point')
    return value


class ScreenArgs(Strict):
    ocr: bool = False
    image: bool = False


class TapArgs(Strict):
    selector: Selector | None = None
    point: str | None = None

    @model_validator(mode='after')
    def one_target(self):
        if (self.selector is None) == (self.point is None):
            raise ValueError('one_target')
        point(self.point)
        return self


class TypeArgs(Strict):
    text: str = Field(min_length=1, max_length=2000)


class KeyArgs(Strict):
    key: Literal['Enter', 'Back', 'Home', 'Backspace', 'Delete', 'Tab', 'Search']


class SwipeArgs(Strict):
    direction: Literal['up', 'down', 'left', 'right'] | None = None
    start: str | None = None
    end: str | None = None
    duration_ms: int = Field(default=400, ge=50, le=5000)

    @model_validator(mode='after')
    def one_form(self):
        if (self.direction is None) == (self.start is None or self.end is None) or (self.start is None) != (self.end is None):
            raise ValueError('one_form')
        point(self.start), point(self.end)
        return self


class WaitArgs(Strict):
    visible: Selector | None = None
    gone: Selector | None = None
    settled: bool | None = None
    timeout_ms: int = Field(default=5000, ge=100, le=30000)

    @model_validator(mode='after')
    def one_condition(self):
        if sum(item is not None for item in (self.visible, self.gone, self.settled)) != 1:
            raise ValueError('one_condition')
        return self


class Region(Strict):
    x: int = Field(ge=0, le=16384)
    y: int = Field(ge=0, le=16384)
    w: int = Field(ge=1, le=16384)
    h: int = Field(ge=1, le=16384)


class ReadTextArgs(Strict):
    selector: Selector | None = None
    region: Region | None = None
    limit: int = Field(default=100, ge=1, le=300)

    @model_validator(mode='after')
    def one_scope(self):
        if self.selector is not None and self.region is not None:
            raise ValueError('one_scope')
        return self


class ReadListArgs(Strict):
    map: str | None = None
    list: str | None = None
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode='after')
    def both_or_none(self):
        if (self.map is None) != (self.list is None):
            raise ValueError('both_or_none')
        return self


class OpenAppArgs(Strict):
    package: Package


class RunFlowArgs(Strict):
    name: str | None = None
    version: str | None = None
    yaml: str | None = Field(default=None, max_length=32768)
    params: dict[str, str | int | bool] = Field(default_factory=dict, max_length=20)


class FlowEmptyArgs(Strict):
    pass


class FlowEditArgs(Strict):
    yaml: str = Field(min_length=1, max_length=32768)


class FinishArgs(Strict):
    summary: str = Field(min_length=1, max_length=2000)
    data: dict[str, object] = Field(default_factory=dict)


MODELS = {'screen': ScreenArgs, 'tap': TapArgs, 'type': TypeArgs, 'key': KeyArgs, 'swipe': SwipeArgs, 'wait': WaitArgs,
          'read_text': ReadTextArgs, 'read_list': ReadListArgs, 'open_app': OpenAppArgs, 'run_flow': RunFlowArgs,
          'flow_get': FlowEmptyArgs, 'flow_edit': FlowEditArgs, 'flow_test': FlowEmptyArgs, 'finish': FinishArgs}
assert set(MODELS) == set(TOOLS)


def target(tool, args):
    """A short, secret-free summary of what an action aims at, for the progress events."""
    if args.get('selector') or args.get('visible') or args.get('gone'):
        chosen = args.get('selector') or args.get('visible') or args.get('gone')
        return ('selector ' + ','.join(f'{key}={value}' for key, value in chosen.items()))[:120]
    if tool == 'type':
        return 'secret ' + args['text'] if REFERENCE.fullmatch(args['text']) else f"{len(args['text'])} characters"
    for key in ('point', 'key', 'package', 'direction', 'name'):
        if args.get(key):
            return f'{key} {args[key]}'[:120]
    if tool == 'run_flow':
        return 'inline flow'
    return ''
