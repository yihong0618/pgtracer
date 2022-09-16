"""
This module contains definitions for representing PostgreSQL queries.
"""
from __future__ import annotations

import ctypes as ct
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, Optional

from ..ebpf.unwind import UnwindAddressSpace
from ..utils import timespec_to_timedelta
from .plan import PlanState

if TYPE_CHECKING:
    from ..ebpf.collector import planstate_data, portal_data
    from ..ebpf.dwarf import ProcessMetadata


FUNCTION_ARGS_MAPPING = {
    "ExecProcNodeFirst": 1,
    "ExecProcNodeInstr": 1,
    "ExecProcNode": 1,
    "ExecAgg": 1,
    "ExecAppend": 1,
    "ExecBitmapAnd": 1,
    "ExecBitmapHeapScan": 1,
    "ExecBitmapIndexScan": 1,
    "ExecBitmapOr": 1,
    "ExecCteScan": 1,
    "ExecCustomScan": 1,
    "ExecForeignScan": 1,
    "ExecFunctionScan": 1,
    "ExecGather": 1,
    "ExecGatherMerge": 1,
    "ExecGroup": 1,
    "ExecHash": 1,
    "ExecHashJoin": 1,
    "ExecIncrementalSort": 1,
    "ExecIndexOnlyScan": 1,
    "ExecIndexScan": 1,
    "ExecLimit": 1,
    "ExecLockRows": 1,
    "ExecMaterial": 1,
    "ExecMemoize": 1,
    "ExecMergeAppend": 1,
    "ExecMergeJoin": 1,
    "ExecModifyTable": 1,
    "ExecNamedTuplestoreScan": 1,
    "ExecNestLoop": 1,
    "ExecProjectSet": 1,
    "ExecRecursiveUnion": 1,
    "ExecResult": 1,
    "ExecSampleScan": 1,
    "ExecSeqScan": 1,
    "ExecSetOp": 1,
    "ExecSort": 1,
    "ExecSubqueryScan": 1,
    "ExecTableFuncScan": 1,
    "ExecTidRangeScan": 1,
    "ExecTidScan": 1,
    "ExecUnique": 1,
    "ExecValuesScan": 1,
    "ExecWindowAgg": 1,
    "ExecWorkTableScan": 1,
    "MultiExecHash": 1,
    "MultiExecBitmapIndexScan": 1,
    "MultiExecBitmapAnd": 1,
    "MultiExecBitmapOr": 1,
}


class Query:
    """
    A PostgreSQL Query.
    """

    def __init__(
        self,
        *,
        startts: Optional[float] = None,
        text: Optional[str] = None,
        # Instrumentation is dynamically generated class, no way to check it
        instrument: Any = None,
        search_path: Optional[str] = None,
    ):
        self.startts = startts
        self.text = text
        self.instrument = instrument
        self.search_path = search_path
        self.nodes: Dict[int, PlanState] = {}

    @property
    def root_node(self) -> PlanState:
        """
        Returns the plan's root node.
        """
        root_candidates = [
            node for node in self.nodes.values() if node.parent_node is None
        ]
        if len(root_candidates) != 1:
            raise ValueError(
                f"Invalid plan, we have {len(root_candidates)} roots when we expect 1"
            )
        return root_candidates[0]

    @classmethod
    def from_event(cls, metadata: ProcessMetadata, event: portal_data) -> Query:
        """
        Build a query from portal_data event generated by eBPF.
        """
        instrument_addr = ct.addressof(event.instrument)
        instrument = metadata.structs.Instrumentation(instrument_addr)
        search_path = None
        if event.search_path:
            search_path = event.search_path.decode("utf8")
        return cls(
            startts=event.portal_key.creation_time,
            text=event.query.decode("utf8"),
            instrument=instrument,
            search_path=search_path,
        )

    def update(self, metadata: ProcessMetadata, event: portal_data) -> None:
        """
        Update the query from an eBPF portal_data event.
        """
        instrument_addr = ct.addressof(event.instrument)
        instrument = metadata.structs.Instrumentation(instrument_addr)
        if instrument.running:
            self.instrument = instrument
        self.startts = event.portal_key.creation_time or self.startts
        self.text = event.query.decode("utf-8") or self.text
        search_path = event.search_path.decode("utf8")
        self.search_path = search_path or self.search_path

    @property
    def start_datetime(self) -> Optional[datetime]:
        """
        Returns the creation timestamp of the portal associated to this query.
        """
        if self.startts is None:
            return None
        return datetime.fromtimestamp(self.startts / 1000000)

    @property
    def runtime(self) -> Optional[timedelta]:
        """
        Returns the query's top-node total runtime.
        """
        if self.instrument:
            return timespec_to_timedelta(self.instrument.counter)
        return None

    def add_node_from_event(
        self, metadata: ProcessMetadata, event: planstate_data
    ) -> PlanState:
        """
        Add a node from planstate_data event to this query plantree.
        We walk the stack up to understand where the nodes are located relative
        to each other.
        """
        nodes = self.nodes
        addr_space = UnwindAddressSpace(event.stack_capture, metadata)
        addr = event.planstate_addr
        planstate = nodes.get(addr)
        if planstate is None:
            planstate = PlanState(addr)
            nodes[addr] = planstate
        planstate.update(metadata, event)
        if not planstate.is_stub:
            return planstate
        cur_node = planstate
        for idx, frame in enumerate(addr_space.frames()):
            # First frame is ours, so skip it
            if idx == 0:
                continue
            if frame.function_name in FUNCTION_ARGS_MAPPING:
                argnum = FUNCTION_ARGS_MAPPING[frame.function_name]
                parent_addr = frame.fetch_arg(argnum, ct.c_ulonglong).value
                if parent_addr == cur_node.addr:
                    continue
                parent_node = nodes.get(parent_addr)
                if parent_node is None:
                    parent_node = PlanState(parent_addr)
                    nodes[parent_addr] = parent_node
                cur_node.parent_node = parent_node
                parent_node.children.add(cur_node)
                # The parent_node is already not a stub, meaning its ancestors
                # have been resolved. Stop walking the frame here
                if not parent_node.is_stub:
                    break
                cur_node = parent_node
        planstate.is_stub = False
        return planstate
