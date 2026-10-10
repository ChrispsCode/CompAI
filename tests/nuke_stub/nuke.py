"""
Fake `nuke` module for running the Nuke-side code outside Nuke.

It models just enough (nodes, knobs, groups, formats, main-thread calls) to
exercise our own logic. It proves nothing about the real Nuke API: anything
that passes here still needs testing in Nuke.
"""

import os

STARTLINE = 1

messages = []          # everything passed to nuke.message()
_this = {"node": None, "knob": None}
_stack = []            # group context, innermost last
_top = {}              # top-level nodes by name
_frame = 1001


def reset():
    del messages[:]
    del _stack[:]
    _top.clear()
    _menu.commands.clear()
    _root.script = "Root"


class Format(object):
    def __init__(self, w=2048, h=1080, pa=1.0, name="2K_DCP"):
        self._w, self._h, self._pa, self._name = w, h, pa, name

    def width(self):
        return self._w

    def height(self):
        return self._h

    def pixelAspect(self):
        return self._pa

    def name(self):
        return self._name


class Knob(object):
    def __init__(self, name="", label="", value=None):
        self._name, self._value = name, value
        self._enabled, self.expression, self.tooltip = True, None, None
        self.node = None

    def name(self):
        return self._name

    def value(self):
        return self._value

    def setValue(self, v):
        self._value = v

    def evaluate(self):
        return self._value

    def fromUserText(self, v):
        self._value = v

    def setExpression(self, e):
        self.expression = e

    def setTooltip(self, t):
        self.tooltip = t

    def setFlag(self, f):
        pass

    def clearFlag(self, f):
        pass

    def setRange(self, a, b):
        pass

    def setEnabled(self, e):
        self._enabled = bool(e)

    def enabled(self):
        return self._enabled

    def values(self):
        return ["sRGB", "linear"]

    def execute(self):
        pass


class Text_Knob(Knob):
    def __init__(self, name, label="", value=""):
        Knob.__init__(self, name, label, value)


_formats = {"2K_DCP": Format(), "HD_1080": Format(1920, 1080, 1.0, "HD_1080")}


def addFormat(spec):
    w, h, pa, name = spec.split()
    _formats[name] = Format(int(w), int(h), float(pa), name)


class Format_Knob(Knob):
    def __init__(self, name, label=""):
        Knob.__init__(self, name, label, _formats["2K_DCP"])

    def setValue(self, v):
        # Like real Nuke: only the name of a registered format is accepted.
        if isinstance(v, str) and v in _formats:
            self._value = _formats[v]


class Enumeration_Knob(Knob):
    def __init__(self, name, label, items):
        Knob.__init__(self, name, label, items[0])
        self._items = list(items)

    def values(self):
        return list(self._items)

    def setValues(self, items):
        self._items = list(items)

    def setValue(self, v):
        if v in self._items:           # like real Nuke: unknown is ignored
            self._value = v


class PyScript_Knob(Knob):
    def __init__(self, name, label="", script=""):
        Knob.__init__(self, name, label, script)

    def execute(self):
        _this["node"] = self.node
        exec(self._value, {"nuke": __import__("nuke")})


class Boolean_Knob(Knob):
    def __init__(self, name, label="", value=False):
        Knob.__init__(self, name, label, value)


class Int_Knob(Knob):
    def __init__(self, name, label="", value=0):
        Knob.__init__(self, name, label, value)


class String_Knob(Knob):
    def __init__(self, name, label="", value=""):
        Knob.__init__(self, name, label, value)


Tab_Knob = Double_Knob = Knob
File_Knob = Multiline_Eval_String_Knob = String_Knob

_AUTO_KNOBS = ("before after on_error resize filter black_outside format pbb "
               "type box_fixed box_width box_height file_type channels "
               "datatype create_directories disable colorspace file reload "
               "tile_color knobChanged").split()


class Node(object):
    def __init__(self, cls, name=None, inputs=None):
        self.cls = cls
        self._name = name or cls + "1"
        self._knobs = {}
        self._inputs = list(inputs or [])
        self.children = {}
        self.parent = _stack[-1] if _stack else None
        self._format = Format()
        (self.parent.children if self.parent else _top)[self._name] = self

    def __getitem__(self, k):
        if k not in self._knobs:
            if k not in _AUTO_KNOBS:
                raise NameError("knob %s.%s does not exist" % (self._name, k))
            self.addKnob(Knob(k))
        return self._knobs[k]

    def knob(self, k):
        return self._knobs.get(k)

    def knobs(self):
        for k in _AUTO_KNOBS:
            self[k]
        return self._knobs

    def addKnob(self, knob):
        knob.node = self
        self._knobs[knob.name()] = knob

    def name(self):
        return self._name

    def fullName(self):
        return self._name

    def setName(self, name):
        owner = self.parent.children if self.parent else _top
        owner.pop(self._name, None)
        self._name = name
        owner[name] = self

    def inputs(self):
        return len(self._inputs)

    def input(self, i):
        return self._inputs[i]

    def setInput(self, i, node):
        while len(self._inputs) <= i:
            self._inputs.append(None)
        self._inputs[i] = node

    def format(self):
        return self._format

    def begin(self):
        _stack.append(self)

    def end(self):
        _stack.pop()

    __enter__ = begin

    def __exit__(self, *a):
        self.end()


class _Nodes(object):
    def __getattr__(self, cls):
        return lambda **kw: Node(cls, kw.get("name"), kw.get("inputs"))


nodes = _Nodes()


def createNode(cls, inpanel=True):
    return Node(cls)


def toNode(name):
    return (_stack[-1].children if _stack else _top).get(name)


def allNodes(cls=None):
    pool = (_stack[-1].children if _stack else _top).values()
    return [n for n in pool if cls is None or n.cls == cls]


def thisNode():
    return _this["node"]


def thisKnob():
    return _this["knob"]


def fire_knob_changed(node, knob_name):
    """Test helper: run the node's knobChanged script as Nuke would."""
    _this["node"], _this["knob"] = node, Knob(knob_name)
    exec(node["knobChanged"].value(), {"nuke": __import__("nuke")})


def message(text):
    messages.append(text)


def executeInMainThread(fn, args=()):
    fn(*args)


def frame():
    return _frame


def execute(write, first, last):
    path = write["file"].value()
    with open(path, "wb") as f:
        f.write(b"fake png")


class _Root(object):
    script = "Root"

    def name(self):
        return self.script


_root = _Root()


def root():
    return _root


class _Menu(object):
    def __init__(self):
        self.commands = {}

    def addMenu(self, name, icon=None):
        return self

    def addCommand(self, name, command):
        self.commands[name] = command

    def run(self, name):
        """Test helper: run a string command as Nuke would."""
        exec(self.commands[name], {"nuke": __import__("nuke")})


_menu = _Menu()


def menu(name):
    return _menu


def pluginAddPath(path):
    pass
