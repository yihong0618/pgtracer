"""
Workhorse for pgtracer.

The BPFCollector works by combining two things:
    - an ebpf program loaded in to the kernel, which is built on the fly
    - DWARF information extracted from the executable (or a separate debug
      symbols file).
"""
from __future__ import annotations

import ctypes as ct
from enum import IntEnum
from pathlib import Path
from typing import Dict, Optional, Type

from bcc import BPF
from psutil import Process

from .dwarf import ProcessMetadata
from .query import Query


def intenum_to_c(intenum: Type[IntEnum]) -> str:
    """
    Generate C code defining an enum corresponding to a Python IntEnum.
    """
    buf = f"enum {intenum.__name__} {{\n"
    members = []

    for member in intenum:
        members.append(f"{intenum.__name__}{member.name} = {member.value}")
    buf += ",\n".join(members)
    buf += "\n};\n"

    return buf


def defines_dict_to_c(defines_dict: dict) -> str:
    """
    Generate a string of C #define directives from a mapping.
    """
    return (
        "\n".join(f"#define {key} {value}" for key, value in defines_dict.items())
        + "\n"
    )


CODE_BASE_PATH = Path(__file__).parent / "code"


def load_c_file(filename: str) -> str:
    """
    Loads a C file from the package code directory.
    """
    filepath = CODE_BASE_PATH / filename
    with filepath.open() as cfile:
        return cfile.read()


# pylint: disable=invalid-name
class EventType(IntEnum):
    """
    EventTypes generated by the EBPF code.
    """

    ExecutorStart = 1
    ExecutorFinish = 2
    DropPortalEnter = 3
    DropPortalReturn = 4


class portal_key(ct.Structure):
    """
    Maps the EBPF-defined struct "portal_key".
    This struct acts a key for a given portal instance, identified by it's pid
    and creation_time.
    """

    _fields_ = [("pid", ct.c_ulong), ("creation_time", ct.c_ulong)]

    def as_tuple(self):
        """
        Returns the struct as tuple.
        """
        return self.pid, self.creation_time


# This is NOT the actual struct definition, as it will be replaced dynamically
# (for fields length). But it helps to have it here as a base.
class portal_data(ct.Structure):
    """
    Represents the portal_data associated to a portal.
    """

    _fields_ = [
        ("event_type", ct.c_short),
        ("portal_key", portal_key),
        ("query", ct.c_char * 2048),
        (
            "instrument",
            ct.c_byte * 0,
        ),
        ("search_path", ct.c_char * 1024),
    ]


class EventHandler:
    """
    Base class for handling events.

    The handle_event method dispatched to handle_{EventType} methods if they
    exist. This acts mostly as a namespace to not pollute the BPFCollector
    class itself.
    """

    def __init__(self):
        self.query_cache = {}
        self.query_history = []
        self.last_portal_key = None

    def handle_event(
        self, bpf_collector: BPF_Collector, event: ct.c_void_p
    ) -> Optional[int]:
        """
        Handle an event from EBPF ringbuffer.
        Every event should be tagged with a short int as the first member to
        handle it's type. It is then dispatched to the appropriate method,
        which will be able to make sense of the actual struct.
        """
        # All events should be tagged with the event's type
        event_type = ct.cast(event, ct.POINTER(ct.c_short)).contents.value
        event_type_name = EventType(event_type).name
        method_name = f"handle_{event_type_name}"
        method = getattr(self, method_name)

        if method:
            return method(bpf_collector, event)

        return None

    def handle_ExecutorStart(self, bpf_collector, event) -> int:
        """
        Handle ExecutorStart event. This event is produced by an uprobe on
        standard_ExecutorStart. See executorstart_enter in program.c.

        We record the fact that a query started, extracting relevant metadata
        already present at the query start.
        """
        event = ct.cast(event, ct.POINTER(portal_data)).contents
        key = event.portal_key.as_tuple()

        if key not in self.query_cache:
            self.query_cache[key] = Query.from_event(bpf_collector.metadata, event)
        else:
            self.query_cache[key].update(bpf_collector.metadata, event)
        return 0

    def handle_ExecutorFinish(self, bpf_collector, event) -> int:
        """
        Handle ExecutorFinish event.
        """
        event = ct.cast(event, ct.POINTER(portal_data)).contents
        key = event.portal_key.as_tuple()
        if key in self.query_cache:
            self.query_cache[event.portal_key.as_tuple()].update(
                bpf_collector.metadata, event
            )
        return 0

    def handle_DropPortalEnter(self, bpf_collector, event) -> int:
        """
        Handle DropPortalEnter event. This event is produced by a uprobe on
        DropPortal. See protaldrop_enter in program.c.

        PortalDrop is called whenever a query is finished: once the last row
        has been read in the case of a single query, or when the cursor is
        closed in the case of a cursor.

        Since PortalDrop is responsbile for cleaning up the portal, we record
        the instrumentation and other data about the query here, and remember
        it's identifier. Only once we return from DropPortal will we actually
        clean up the query from our current cache, and append it to history.
        """
        event = ct.cast(event, ct.POINTER(portal_data)).contents
        self.last_portal_key = event.portal_key.as_tuple()
        if self.last_portal_key in self.query_cache:
            self.query_cache[self.last_portal_key].update(bpf_collector.metadata, event)
        return 0

    def handle_DropPortalReturn(self, _, event) -> int:
        """
        Handle DropPortalReturn event. This event is produced by an uretprobe on
        DropPortal. See protaldrop_return in program.c.

        We remove the query from the internal cache  and append it to history.
        """
        event = ct.cast(event, ct.POINTER(portal_data)).contents

        if self.last_portal_key is not None:
            if self.last_portal_key in self.query_cache:
                query = self.query_cache[self.last_portal_key]
                self.query_history.append(query)
                del self.query_cache[self.last_portal_key]
            self.last_portal_key = None
        return 0


class BPF_Collector:
    """
    Workhorse for pgtracer.

    This class allows the user to load an EBPF program dynamically generated
    using supplied options and extracted metadata about the Postgres
    executable.
    """

    def __init__(self, pid: int, instrument_options=None):
        self.pid = pid
        self.process = Process(self.pid)
        self.program = self.process.exe()
        self.metadata = ProcessMetadata(self.process)
        self.instrument_options = instrument_options
        self.bpf = self.prepare_bpf()
        self.event_handler = EventHandler()
        self.update_struct_defs()
        self._started = False

    def update_struct_defs(self) -> None:
        """
        Update the ctypes struct definitions from the DWARF metadata.

        Some C structs used in EBPF must match what is defined by Postgres:
        so we build the class dynamically after the DWARF file has been loaded.
        """
        # Update global struct definitions with actual sizes
        global portal_data  # pylint: disable=global-statement
        portal_data = type(  # type: ignore
            "portal_data",
            (ct.Structure,),
            {
                "_fields_": [
                    ("event_type", ct.c_short),
                    ("portal_key", portal_key),
                    ("query", ct.c_char * 2048),
                    (
                        "instrument",
                        ct.c_byte * self.metadata.structs.Instrumentation.size(),
                    ),
                    ("search_path", ct.c_char * 1024),
                ]
            },
        )

    @property
    def constant_defines(self) -> dict:
        """
        Returns a list of constants to add to the ebpf program as #define
        directives.
        """
        constants = {
            "PID": self.pid,
            "STACK_TOP_ADDR": self.metadata.stack_top,
            # TODO: find a way to extract those ?
            "POSTGRES_EPOCH_JDATE": 2451545,
            "UNIX_EPOCH_JDATE": 2440588,
            "SECS_PER_DAY": 86400,
            # TODO: make those configurable ?
            "MAX_QUERY_NUMBER": 10,
            "MAX_QUERY_LENGTH": 2048,
            "MAX_STACK_READ": 4096,
            "MAX_SEARCHPATH_LENGTH": 1024,
            "EVENTRING_PAGE_SIZE": 1024,
        }

        # USER_INSTRUMENT_OPTIONS is defined only if the user wants to
        # inconditonally turn on instrumentation.
        if self.instrument_options:
            constants["USER_INSTRUMENT_OPTIONS"] = self.instrument_options

        return constants

    @property
    def struct_offsets_defines(self) -> Dict[str, int]:
        """
        Build C-Code for the eBPF code to easily access named members in
        structs.

        We read the offset in a struct for known members, so that the eBPF code
        can read those members from the Postgres struct.

        This is necessary because we can't include Postgres headers in the eBPF
        code.
        """
        # Returns a normalized way of DEFINING struct offsets
        s = self.metadata.structs

        return {
            f"STRUCT_{struct}_OFFSET_{member}": getattr(s, struct)
            .field_definition(member)
            .offset
            for struct, member in (
                ("Node", "type"),
                ("PortalData", "queryDesc"),
                ("PortalData", "creation_time"),
                ("QueryDesc", "sourceText"),
                ("QueryDesc", "instrument_options"),
                ("QueryDesc", "planstate"),
                ("PlanState", "instrument"),
            )
        }

    def make_global_variables_enum(self) -> Type[IntEnum]:
        """
        Create an IntEnum mapping global variables names to their address in
        the program.
        """
        mapping = {}

        for key in ("ActivePortal", "namespace_search_path"):
            mapping[key] = self.metadata.global_variable(key)
        # Mypy complains about dynamic enums
        globalenum = IntEnum("GlobalVariables", mapping)  # type: ignore

        return globalenum

    def make_struct_sizes_dict(self) -> dict:
        """
        Create a dictionary mapping struct name to their bytesize.

        Once again, this is because we can't include Postgres header and call
        "sizeof".
        """
        mapping = {}

        for key in ("Instrumentation",):
            mapping[f"STRUCT_SIZE_{key}"] = getattr(self.metadata.structs, key).size()

        return mapping

    def _attach_uprobe(self, function_name: str, ebpf_function: str) -> None:
        """
        Helper to attach a uprobe executing `ebpf_function` at every
        `function_name` location.
        """
        for addr in self.metadata.function_addresses(function_name):
            self.bpf.attach_uprobe(
                name=self.program, fn_name=ebpf_function, addr=addr, pid=self.pid
            )

    def _attach_uretprobe(self, function_name, ebpf_function):
        """
        Helper to attach a uretprobe executing `ebpf_function` at every
        `function_name` location.
        """
        # TODO: make sure multiple addresses work too
        for addr in self.metadata.function_addresses(function_name):
            self.bpf.attach_uretprobe(
                name=self.program,
                fn_name=ebpf_function,
                addr=addr,
                pid=self.pid,
            )

    def start(self):
        """
        Start the ebpf collector:
         - attach uprobes/uretprobes
         - open the ringbuffer.
        """
        print("Starting eBPF collector...")
        self.bpf["event_ring"].open_ring_buffer(self._handle_event)
        self._attach_uprobe("PortalDrop", "portaldrop_enter")
        self._attach_uretprobe("PortalDrop", "portaldrop_return")
        self._attach_uprobe("standard_ExecutorStart", "executorstart_enter")
        self._attach_uprobe("ExecutorFinish", "executorfinish_enter")
        self._started = True
        print("eBPF collector started")

    def _handle_event(self, cpu, data, size):  # pylint: disable=unused-argument
        """
        Callback for the ring_buffer_poll. We actually dispatch this to the
        `EventHandler`
        """
        return self.event_handler.handle_event(self, data)

    def prepare_bpf(self) -> BPF:
        """
        Generate the eBPF program, both from static code and dynamically
        generated defines and enums.
        """
        print("Generating eBPF source program...")
        buf = defines_dict_to_c(self.constant_defines)
        buf += defines_dict_to_c(self.struct_offsets_defines)
        buf += defines_dict_to_c(self.make_struct_sizes_dict())
        buf += intenum_to_c(EventType)
        buf += intenum_to_c(self.make_global_variables_enum())
        buf += load_c_file("program.c")
        # Add the code directory as include dir
        cflags = [f"-I{CODE_BASE_PATH}"]
        # Suppress some common warnings depending on bcc / kernel combinations
        cflags.append("-Wno-macro-redefined")
        cflags.append("-Wno-ignored-attributes")
        print("Compiling eBPF program...")
        bpf = BPF(text=buf, cflags=cflags, debug=0)
        print("eBPF program compiled")
        return bpf

    def poll(self, timeout=-1) -> None:
        """
        Wrapper around ring_buffer_poll: the first time we're called, we attach
        the probes.
        """
        if not self._started:
            self.start()
        self.bpf.ring_buffer_poll(timeout)
