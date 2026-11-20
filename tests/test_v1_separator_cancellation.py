"""Regression tests for scoped V1 separator cancellation and deadlines.

All external work is replaced with mocks: these tests never invoke FFmpeg,
Demucs, a model, or a GPU.
"""
import asyncio
import contextlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import batch_control
import shared_state
import video_utils
from backend.ai import v1_asr_isolated
from backend.v1_orchestrator import V1Orchestrator
import backend.v1_stage_runtime as v1_stage_runtime
from backend.v1_stage_runtime import await_stage
from backend.v1_retry_policy import is_transient_network_error, should_retry_job


class _AsyncNoopLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Pipe:
    def __init__(self, lines=()):
        self._lines = iter(lines)

    def readline(self):
        return next(self._lines, "")

    def close(self):
        pass


class _FakeProcess:
    """Popen double which records termination and exposes reap operations."""
    pid = 4812

    def __init__(self, stdout_lines=()):
        self.stdout = _Pipe(stdout_lines)
        self.stderr = _Pipe()
        self.returncode = None
        self.terminate_called = False
        self.kill_called = False
        self.wait_calls = []
        self.communicate_calls = []
        self.tree_killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_called = True

    def kill(self):
        self.kill_called = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.returncode is None and timeout is not None:
            raise subprocess.TimeoutExpired("separator", timeout)
        if self.returncode is None:
            self.returncode = -9
        return self.returncode

    def communicate(self, timeout=None):
        self.communicate_calls.append(timeout)
        if timeout is not None and self.returncode is None:
            raise subprocess.TimeoutExpired("separator", timeout)
        return "", ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class _CapturedClock(float):
    """Float clock value that records the duration added for a deadline."""
    def __new__(cls, value, captures):
        obj = super().__new__(cls, value)
        obj.captures = captures
        return obj

    def __add__(self, other):
        self.captures.append(float(other))
        return _CapturedClock(float(self) + float(other), self.captures)


class StageCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_465_second_local_stage_timeout_propagates_without_retry(self):
        local_attempts = []

        def local_worker():
            local_attempts.append(1)
            predicate = batch_control.stop_check.get()
            while not predicate():
                import time
                time.sleep(0.001)
            return "stopped"

        async def stage_work():
            return await asyncio.to_thread(local_worker)

        fake_loop = SimpleNamespace(time=Mock(side_effect=[0.0, 465.0]))
        async_api = SimpleNamespace(
            get_running_loop=lambda: fake_loop,
            ensure_future=asyncio.ensure_future,
            wait=asyncio.wait,
            shield=asyncio.shield,
            CancelledError=asyncio.CancelledError,
        )
        with patch.object(v1_stage_runtime, "asyncio", async_api):
            with self.assertRaisesRegex(TimeoutError, r"Stage exceeded 465s; worker stopped before retry") as caught:
                await await_stage(stage_work(), timeout=465)

        self.assertEqual(fake_loop.time.call_count, 2)
        self.assertEqual(local_attempts, [1])
        self.assertFalse(should_retry_job(caught.exception))
        self.assertFalse(is_transient_network_error(caught.exception))

    async def test_await_stage_contextvar_reaches_to_thread_and_resets(self):
        inherited = lambda: True
        token = batch_control.stop_check.set(inherited)
        try:
            observed = await await_stage(
                asyncio.to_thread(lambda: batch_control.stop_check.get()()),
                timeout=2.0,
            )
            self.assertTrue(observed)
            self.assertIs(batch_control.stop_check.get(), inherited)
        finally:
            batch_control.stop_check.reset(token)

    async def test_shared_state_stop_signals_worker_through_await_stage_context(self):
        old_local_stop = getattr(shared_state, "_local_stop", False)
        finished = asyncio.Event()
        observed_scoped_stop = []

        def check_shared_stop():
            if shared_state.stop_requested:
                raise RuntimeError("shared stop requested")

        async def worker():
            def blocking_worker():
                predicate = batch_control.stop_check.get()
                for _ in range(200):
                    if predicate is not None and predicate():
                        observed_scoped_stop.append(True)
                        return "worker-stopped"
                    import time
                    time.sleep(0.005)
                return "worker-did-not-see-stop"

            result = await asyncio.to_thread(blocking_worker)
            finished.set()
            return result

        try:
            shared_state._local_stop = True
            with self.assertRaisesRegex(RuntimeError, "shared stop requested"):
                await asyncio.wait_for(await_stage(worker(), timeout=2.0, check_stop=check_shared_stop), 3.0)
            self.assertTrue(finished.is_set(), "await_stage must drain its worker before returning")
            self.assertEqual(observed_scoped_stop, [True])
        finally:
            shared_state._local_stop = old_local_stop


class SeparatorSubprocessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input_audio = self.root / "input.wav"
        self.input_audio.write_bytes(b"mock audio")
        self.output = self.root / "out"
        self.output.mkdir()
        self.old_local_stop = getattr(shared_state, "_local_stop", False)
        shared_state._local_stop = False

    def tearDown(self):
        shared_state._local_stop = self.old_local_stop
        self.temp.cleanup()

    def _patch_separator_dependencies(self, *, process_factory=None, run_side_effect=None, duration=0.0):
        policy = SimpleNamespace(
            separator_python_path=lambda: Path(sys.executable),
            separator_model="mock-separator.ckpt",
            demucs_model="htdemucs_ft",
        )
        stack = contextlib.ExitStack()
        stack.enter_context(patch("ai.v1_model_policy.current_v1_model_policy", return_value=policy))
        stack.enter_context(patch("v1_separator_lock.separator_gpu_lock", side_effect=lambda **_kw: contextlib.nullcontext()))
        stack.enter_context(patch.dict(sys.modules, {"soundfile": SimpleNamespace(info=lambda _p: SimpleNamespace(duration=duration))}))
        # Only model/worker presence checks are faked; the input fixture remains a real temp file.
        stack.enter_context(patch.object(video_utils.os.path, "isfile", return_value=True))
        if process_factory is not None:
            stack.enter_context(patch.object(video_utils.subprocess, "Popen", side_effect=process_factory))
        if run_side_effect is not None:
            stack.enter_context(patch.object(video_utils.subprocess, "run", side_effect=run_side_effect))
        return stack

    def test_explicit_timeout_is_not_extended_by_audio_duration(self):
        captured = []
        vocals = str(self.root / "vocals.wav")
        bgm = str(self.root / "background.wav")
        stdout = json.dumps({"success": True, "vocals_path": vocals, "background_path": bgm})
        process = _FakeProcess([stdout + "\n"])
        process.returncode = 0
        clock_values = iter([_CapturedClock(100.0, captured)])
        with self._patch_separator_dependencies(
            process_factory=lambda *_a, **_kw: process, duration=3600.0
        ) as stack:
            stack.enter_context(patch.object(video_utils.time, "monotonic", side_effect=lambda: next(clock_values)))
            result = video_utils.separate_vocals_demucs(
                str(self.input_audio), str(self.output),
                timeout_seconds=7.25, separation_mode="roformer",
            )
        self.assertEqual(result, (vocals, bgm))
        self.assertEqual(captured, [7.25], "long audio must not extend an explicit local deadline")
        self.assertEqual(process.returncode, 0)

    def test_default_timeout_still_scales_up_for_long_audio(self):
        captured = []
        vocals = str(self.root / "default-vocals.wav")
        bgm = str(self.root / "default-background.wav")
        stdout = json.dumps({"success": True, "vocals_path": vocals, "background_path": bgm})
        process = _FakeProcess([stdout + "\n"])
        process.returncode = 0
        with self._patch_separator_dependencies(
            process_factory=lambda *_a, **_kw: process, duration=3600.0
        ) as stack:
            stack.enter_context(patch.object(
                video_utils.time, "monotonic",
                side_effect=lambda: _CapturedClock(100.0, captured),
            ))
            result = video_utils.separate_vocals_demucs(
                str(self.input_audio), str(self.output), separation_mode="roformer",
            )
        self.assertEqual(result, (vocals, bgm))
        self.assertEqual(captured, [9120.0], "omitted timeout retains duration-aware scaling")

    def test_successful_worker_json_stdout_is_parsed(self):
        vocals = str(self.root / "json-vocals.wav")
        bgm = str(self.root / "json-background.wav")
        payload = {
            "success": True,
            "vocals_path": vocals,
            "background_path": bgm,
            "device": "mock-gpu",
            "fp_mode": "native_fp16",
        }
        process = _FakeProcess(["worker log line\n", json.dumps(payload) + "\n"])
        process.returncode = 0
        with self._patch_separator_dependencies(process_factory=lambda *_a, **_kw: process):
            result = video_utils.separate_vocals_demucs(
                str(self.input_audio), str(self.output),
                timeout_seconds=12, separation_mode="roformer",
            )
        self.assertEqual(result, (vocals, bgm))
        self.assertEqual(process.returncode, 0)

    def test_scoped_cancel_before_launch_does_not_acquire_gpu_or_spawn_worker(self):
        with self._patch_separator_dependencies() as stack:
            launch = stack.enter_context(patch.object(video_utils.subprocess, "Popen"))
            cuda_or_cleanup = stack.enter_context(patch.object(video_utils.subprocess, "run"))
            gpu_lock = stack.enter_context(patch(
                "v1_separator_lock.separator_gpu_lock",
                side_effect=lambda **_kw: contextlib.nullcontext(),
            ))
            token = batch_control.stop_check.set(lambda: True)
            try:
                with self.assertRaisesRegex(RuntimeError, "bị hủy trước khi khởi chạy worker") as caught:
                    video_utils.separate_vocals_demucs(
                        str(self.input_audio), str(self.output),
                        timeout_seconds=20, separation_mode="auto",
                    )
            finally:
                batch_control.stop_check.reset(token)

        launch.assert_not_called()
        cuda_or_cleanup.assert_not_called()
        gpu_lock.assert_not_called()
        self.assertFalse(should_retry_job(caught.exception))
        self.assertFalse(is_transient_network_error(caught.exception))

    def test_separator_local_timeout_is_not_network_error_or_retry(self):
        process = _FakeProcess()
        launched = []
        popen = Mock(side_effect=lambda *_a, **_kw: process)

        def mocked_run(command, **_kwargs):
            launched.append(command)
            if "taskkill" in str(command[0]).lower():
                process.tree_killed = True
                process.returncode = -9
            return SimpleNamespace(returncode=0)

        with self._patch_separator_dependencies() as stack:
            stack.enter_context(patch.object(video_utils.subprocess, "Popen", popen))
            stack.enter_context(patch.object(video_utils.subprocess, "run", side_effect=mocked_run))
            stack.enter_context(patch.object(video_utils.time, "monotonic", side_effect=[100.0, 1000.0]))
            with self.assertRaisesRegex(TimeoutError, "Tiến trình tách âm timeout sau 30.0s") as caught:
                video_utils.separate_vocals_demucs(
                    str(self.input_audio), str(self.output),
                    timeout_seconds=30, separation_mode="auto",
                )

        popen.assert_called_once()
        self.assertTrue(process.wait_calls, "timed-out worker must be waited/reaped")
        self.assertFalse(
            any("demucs" in " ".join(map(str, command)).lower() or "import torch" in " ".join(map(str, command)).lower() for command in launched),
            "local separator timeout must not start network/GPU fallback work",
        )
        self.assertFalse(should_retry_job(caught.exception))
        self.assertFalse(is_transient_network_error(caught.exception))

    def test_auto_cleanup_failures_do_not_start_fallback_or_retry(self):
        failures = (
            ("kill_oserror", OSError("mock kill failed")),
            ("final_wait_timeout", subprocess.TimeoutExpired("mock cleanup wait", 2)),
        )
        for failure_mode, cleanup_failure in failures:
            with self.subTest(failure_mode=failure_mode):
                process = _FakeProcess()
                launched = []
                popen = Mock(return_value=process)

                def failed_taskkill(command, **_kwargs):
                    launched.append(command)
                    raise cleanup_failure

                if failure_mode == "kill_oserror":
                    process.kill = Mock(side_effect=OSError("mock process kill failed"))
                else:
                    def failed_final_wait(timeout=None):
                        process.wait_calls.append(timeout)
                        raise subprocess.TimeoutExpired("mock final reap", timeout)
                    process.wait = failed_final_wait

                with self._patch_separator_dependencies() as stack:
                    stack.enter_context(patch.object(video_utils.os, "name", "nt"))
                    stack.enter_context(patch.object(video_utils.subprocess, "Popen", popen))
                    stack.enter_context(patch.object(video_utils.subprocess, "run", side_effect=failed_taskkill))
                    stack.enter_context(patch.object(video_utils.time, "monotonic", side_effect=[100.0, 1000.0]))
                    with self.assertRaisesRegex(RuntimeError, "Không xác minh được cây worker tách âm đã dừng") as caught:
                        video_utils.separate_vocals_demucs(
                            str(self.input_audio), str(self.output),
                            timeout_seconds=30, separation_mode="auto",
                        )

                popen.assert_called_once()
                self.assertTrue(launched, "cleanup failure should originate during process-tree cleanup")
                self.assertFalse(
                    any("demucs" in " ".join(map(str, command)).lower() or "import torch" in " ".join(map(str, command)).lower() for command in launched),
                    "unverified cleanup must not start Demucs or CUDA probes",
                )
                self.assertFalse(should_retry_job(caught.exception))
                self.assertFalse(is_transient_network_error(caught.exception))

    def test_cancelled_running_worker_message_is_not_retryable(self):
        error = RuntimeError("Tác vụ tách âm bị hủy; cây worker đã dừng.")
        self.assertFalse(should_retry_job(error))
        self.assertFalse(is_transient_network_error(error))

    def test_auto_mode_does_not_launch_demucs_after_scoped_worker_cancel(self):
        launched = []
        taskkill_commands = []
        process = _FakeProcess()
        scoped_cancel = {"active": False}

        def popen_factory(*_args, **_kwargs):
            scoped_cancel["active"] = True
            return process

        def fake_run(command, **_kwargs):
            launched.append(command)
            is_taskkill = bool(command) and (
                str(command[0]).lower().endswith("taskkill.exe") or command[0] == "taskkill"
            )
            if is_taskkill:
                taskkill_commands.append(command)
                process.tree_killed = True
                process.returncode = -9
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        token = batch_control.stop_check.set(lambda: scoped_cancel["active"])
        try:
            with self._patch_separator_dependencies(process_factory=popen_factory, run_side_effect=fake_run) as stack:
                stack.enter_context(patch.object(video_utils.os, "name", "nt"))
                # Bound any implementation that regresses to deadline-only polling.
                stack.enter_context(patch.object(video_utils.time, "monotonic", side_effect=[1.0, 1000.0, 1000.0]))
                with self.assertRaises((RuntimeError, TimeoutError)):
                    video_utils.separate_vocals_demucs(
                        str(self.input_audio), str(self.output),
                        timeout_seconds=30, separation_mode="auto",
                    )
        finally:
            batch_control.stop_check.reset(token)

        self.assertTrue(process.tree_killed, "Windows cancellation must kill the separator process tree")
        self.assertTrue(taskkill_commands)
        self.assertIn("/T", taskkill_commands[0])
        self.assertIn("/F", taskkill_commands[0])
        self.assertIn(str(process.pid), taskkill_commands[0])
        self.assertTrue(process.wait_calls or process.communicate_calls, "cancelled child must be waited/reaped")
        self.assertFalse(
            any("demucs" in " ".join(map(str, command)).lower() or "import torch" in " ".join(map(str, command)).lower()
                for command in launched),
            "a scoped abort in auto mode must not start the Demucs fallback or its CUDA probe",
        )

    def test_windows_batch_control_kills_process_tree_and_reaps_child(self):
        process = _FakeProcess()
        cancel = {"requested": False}

        def communicate(timeout=None):
            process.communicate_calls.append(timeout)
            if timeout is not None and not cancel["requested"]:
                cancel["requested"] = True
                raise subprocess.TimeoutExpired("separator", timeout)
            return "", ""

        process.communicate = communicate
        kill_commands = []

        def fake_taskkill(command, **_kwargs):
            kill_commands.append(command)
            process.tree_killed = True
            process.returncode = -9
            return SimpleNamespace(returncode=0)

        token = batch_control.stop_check.set(lambda: cancel["requested"])
        try:
            with patch.object(batch_control.os, "name", "nt"), \
                 patch.object(batch_control.subprocess, "Popen", return_value=process), \
                 patch.object(batch_control.subprocess, "run", side_effect=fake_taskkill), \
                 patch.object(batch_control.subprocess, "CREATE_NO_WINDOW", 0, create=True):
                with self.assertRaisesRegex(RuntimeError, "stop requested"):
                    batch_control.run(["fake-separator"], capture_output=True)
        finally:
            batch_control.stop_check.reset(token)

        self.assertTrue(kill_commands, "Windows cancellation must issue a process-tree kill")
        kill_command = kill_commands[0]
        self.assertIn("/T", kill_command)
        self.assertIn("/F", kill_command)
        self.assertIn(str(process.pid), kill_command)
        self.assertTrue(process.tree_killed)
        self.assertIn(None, process.communicate_calls, "communicate() without timeout reaps captured pipes")


class V1StageTimeoutForwardingTests(unittest.IsolatedAsyncioTestCase):
    def _context(self, out_dir, **overrides):
        values = dict(
            job_id="test-job",
            out_dir=Path(out_dir),
            effective_cfg={"separation_mode": "roformer", "feature_flags": {}},
            planned={
                "asr_model": "whisper_turbo",
                "asr_chunking": False,
                "asr_chunk_size_s": 240.0,
                "asr_overlap_s": 0.0,
            },
            resolved_mode="SHORT",
            overrides={},
            visual_sub_to_speech=False,
            video_dur_s=12.0,
            can_resume=False,
            verified_stages=[],
            artifacts_acc={},
            check_stop=Mock(),
            pause_checkpoint=AsyncMock(),
            notify=AsyncMock(),
        )
        values.update(overrides)
        async def run_stage(awaitable, timeout):
            return await awaitable
        values["run_stage"] = run_stage
        return SimpleNamespace(**values)

    async def test_primary_separator_stage_forwards_timeout_seconds(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            vocals, bgm = root / "vocals.wav", root / "no_vocals.wav"
            vocals.write_bytes(b"v")
            bgm.write_bytes(b"b")
            orchestrator = object.__new__(V1Orchestrator)
            orchestrator.manifest_manager = Mock()
            orchestrator._calculate_stage_timeout = Mock(return_value=47)
            orchestrator._check_gpu_guard = Mock()
            context = self._context(root)
            with patch("v1_gpu_gatekeeper.async_gpu_gatekeeper", side_effect=lambda *_a, **_kw: _AsyncNoopLock()), \
                 patch("video_utils.separate_vocals_demucs", return_value=(str(vocals), str(bgm))) as separate:
                await orchestrator._stage_separate(context, root / "original.wav")
            self.assertEqual(separate.call_args.kwargs.get("timeout_seconds"), 47)

    async def test_vad_recovery_separator_stage_forwards_timeout_seconds(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            vocals, bgm = root / "vocals.wav", root / "no_vocals.wav"
            vocals.write_bytes(b"v")
            bgm.write_bytes(b"b")
            orchestrator = object.__new__(V1Orchestrator)
            orchestrator.manifest_manager = Mock()
            orchestrator._calculate_stage_timeout = Mock(side_effect=lambda stage, *_a: 47 if stage == "separate_vocals" else 19)
            orchestrator._check_gpu_guard = Mock()
            context = self._context(
                root,
                visual_sub_to_speech=True,
                _vad_bypassed_separation=True,
                _vad_saved_sep_mode="roformer",
            )
            context._stage_markers = None
            segments = [SimpleNamespace(index=1, start=0.0, end=1.0, content="hello")]

            def fake_asr(_vocals, srt_path, **_kwargs):
                Path(srt_path).write_text("placeholder", encoding="utf-8")
                return segments

            visual_failure = AsyncMock(side_effect=RuntimeError("no visual transcript"))
            orchestrator._stage_visual_transcript = visual_failure
            with patch("v1_gpu_gatekeeper.async_gpu_gatekeeper", side_effect=lambda *_a, **_kw: _AsyncNoopLock()), \
                 patch("video_utils.separate_vocals_demucs", return_value=(str(vocals), str(bgm))) as separate, \
                 patch.object(v1_asr_isolated, "extract_subtitles_isolated", side_effect=fake_asr), \
                 patch("backend.v1_orchestrator._looks_like_bgm_hallucination", return_value=False):
                await orchestrator._stage_asr(context, root / "input-vocals.wav", root / "original.wav")
            self.assertEqual(separate.call_args.kwargs.get("timeout_seconds"), 47)


if __name__ == "__main__":
    unittest.main()
