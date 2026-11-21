from backend.ffmpeg_encode import run_with_encoder_fallback


def test_fallback_runs_nvenc_then_x264_and_calls_hook():
    commands = []
    before = []

    def run(command):
        commands.append(command)
        return command[0]

    success, encoder = run_with_encoder_fallback(
        lambda args: ["ffmpeg", *args],
        ["h264_nvenc", "-preset", "p4"],
        ["libx264", "-preset", "medium"],
        run=run,
        ok=lambda result: result == "ffmpeg" and len(commands) == 2,
        before_fallback=lambda: before.append(True),
    )

    assert success
    assert encoder == "libx264"
    assert commands == [
        ["ffmpeg", "h264_nvenc", "-preset", "p4"],
        ["ffmpeg", "libx264", "-preset", "medium"],
    ]
    assert before == [True]


def test_nvenc_exception_falls_back_and_stop_or_skip_respects_control():
    commands = []

    def run(command):
        commands.append(command)
        if "h264_nvenc" in command:
            raise RuntimeError("NVENC unavailable")
        return 0

    assert run_with_encoder_fallback(
        lambda args: args,
        ["h264_nvenc"],
        ["libx264"],
        run=run,
        ok=lambda result: result == 0,
    ) == (True, "libx264")
    assert commands == [["h264_nvenc"], ["libx264"]]

    commands.clear()
    assert run_with_encoder_fallback(
        lambda args: args,
        ["h264_nvenc"],
        ["libx264"],
        run=run,
        ok=lambda result: True,
        skip_nvenc=True,
        should_stop=lambda: False,
    ) == (True, "libx264")
    assert commands == [["libx264"]]

    commands.clear()
    assert run_with_encoder_fallback(
        lambda args: args,
        ["h264_nvenc"],
        ["libx264"],
        run=run,
        ok=lambda result: True,
        should_stop=lambda: True,
    ) == (False, "")
    assert commands == []
