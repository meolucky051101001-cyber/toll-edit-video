"""Concrete regressions for incomplete Windows teardown and raising stop callbacks."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import importlib.util
from pathlib import Path

_spec=importlib.util.spec_from_file_location("_separator_test_helpers",Path(__file__).with_name("test_v1_separator_cancellation.py"))
helpers=importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helpers)
import batch_control
import video_utils
from backend.v1_retry_policy import should_retry_job,is_transient_network_error

@pytest.fixture
def separator_case():
    case=helpers.SeparatorSubprocessTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()

@pytest.mark.parametrize("tree_failure",["nonzero","oserror"])
def test_parent_exit_does_not_verify_failed_windows_tree_kill(separator_case,tree_failure):
    process=helpers._FakeProcess()
    def terminate():
        process.terminate_called=True
        process.returncode=-15
    process.terminate=terminate
    commands=[]
    def failed_tree(command,**kwargs):
        commands.append(command)
        if tree_failure=="oserror":
            raise OSError("tree cleanup denied")
        return SimpleNamespace(returncode=5)
    with separator_case._patch_separator_dependencies() as stack:
        stack.enter_context(patch.object(video_utils.os,"name","nt"))
        popen=stack.enter_context(patch.object(video_utils.subprocess,"Popen",return_value=process))
        stack.enter_context(patch.object(video_utils.subprocess,"run",side_effect=failed_tree))
        stack.enter_context(patch.object(video_utils.time,"monotonic",side_effect=[100.,1000.]))
        with pytest.raises(RuntimeError,match="Không xác minh được cây worker") as caught:
            video_utils.separate_vocals_demucs(str(separator_case.input_audio),str(separator_case.output),timeout_seconds=30,separation_mode="auto")
    assert type(caught.value).__name__=="_SeparatorCleanupError"
    assert process.terminate_called and process.wait_calls
    assert popen.call_count==1
    assert all("taskkill" in str(c[0]).lower() for c in commands)
    assert not should_retry_job(caught.value)
    assert not is_transient_network_error(caught.value)

def test_raising_scoped_stop_cannot_launch_demucs(separator_case):
    process=helpers._FakeProcess()
    launched={"active":False}
    def predicate():
        if launched["active"]:
            raise RuntimeError("Batch stop requested")
        return False
    def launch(*args,**kwargs):
        launched["active"]=True
        return process
    commands=[]
    def taskkill(command,**kwargs):
        commands.append(command)
        if "taskkill" in str(command[0]).lower():
            process.returncode=-9
        return SimpleNamespace(returncode=0,stdout="",stderr="")
    token=batch_control.stop_check.set(predicate)
    try:
        with separator_case._patch_separator_dependencies(process_factory=launch,run_side_effect=taskkill) as stack:
            stack.enter_context(patch.object(video_utils.os,"name","nt"))
            stack.enter_context(patch.object(video_utils.time,"monotonic",return_value=100.))
            with pytest.raises(RuntimeError,match="bị hủy") as caught:
                video_utils.separate_vocals_demucs(str(separator_case.input_audio),str(separator_case.output),timeout_seconds=30,separation_mode="auto")
    finally:
        batch_control.stop_check.reset(token)
    assert type(caught.value).__name__=="_SeparatorCancellation"
    assert process.wait_calls
    assert len(commands)==1 and "taskkill" in str(commands[0][0]).lower()
    assert not should_retry_job(caught.value)
    assert not is_transient_network_error(caught.value)
