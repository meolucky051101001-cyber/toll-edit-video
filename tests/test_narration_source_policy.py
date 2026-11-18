from datetime import timedelta
from types import SimpleNamespace as NS
from backend.v1_job_policy import looks_like_bgm_hallucination, visual_transcript_candidates

def test_sparse_valid_speech_is_not_music_hallucination():
    cue = NS(content='这款自封袋非常好用',start=timedelta(seconds=2),end=timedelta(seconds=2.8))
    assert not looks_like_bgm_hallucination([cue],60)
    cue.asr_hallucination = True
    assert looks_like_bgm_hallucination([cue],60)

def test_moving_product_labels_do_not_become_silent_narration():
    label = NS(content='材质|马口铁、纸、PET 生产地址|浙江杭州',visual_sub_to_speech=True)
    caption = NS(content='这个尺寸纯是为小说人量身打造来的',visual_sub_to_speech=True)
    assert visual_transcript_candidates([label,caption]) == [caption]
