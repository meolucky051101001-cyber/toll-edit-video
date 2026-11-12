from unittest.mock import patch
import pytest
from backend.ai import v1_auto_voice as voice


def cues(indexed=True):
    result = [dict(start=i*2., end=i*2.+1., content='reply') for i in range(3)]
    if indexed:
        for i, cue in enumerate(result, 1):
            cue['index'] = i
    return result


def evidence(middle='unknown', reason='too_short'):
    return {i: dict(gender=g, confidence=.95 if g != 'unknown' else 0.,
                    reason=reason if i == 2 else 'sources_agree')
            for i, g in enumerate(['male', middle, 'male'], 1)}


def decide(path, segments):
    return voice.decide_video_voice(path, segments, voice_mode='dialogue', job_id='test-job',
        workspace=str(path.parent), voice_config={'dual_voice':True,
        'male_voice_id':voice.VOICE_MALE_ID, 'female_voice_id':voice.VOICE_FEMALE_ID})


@pytest.mark.parametrize('reason', ['too_short', 'vocals_original_disagree', 'mixed_speaker_cue', 'no_reliable_source'])
def test_neighbours_cannot_override_uncertain_reply(tmp_path, reason):
    with patch.object(voice, 'classify_all_segments_gender', return_value=evidence(reason=reason)):
        locked = decide(tmp_path/'job', cues())
    assert locked['segment_voices']['2']['gender'] == 'unknown'
    assert locked['segment_voices']['2']['reason'] == reason
    assert locked['requires_voice_review']


def test_short_female_reply_keeps_female_voice_without_indices(tmp_path):
    with patch.object(voice, 'classify_all_segments_gender', return_value=evidence('female')):
        locked = decide(tmp_path/'job', cues(False))
    assert list(locked['segment_voices']) == ['1','2','3']
    assert locked['segment_voices']['1']['id'] == voice.VOICE_MALE_ID
    assert locked['segment_voices']['2']['id'] == voice.VOICE_FEMALE_ID
    assert locked['segment_voices']['3']['id'] == voice.VOICE_MALE_ID
    assert not locked['requires_voice_review']


@pytest.mark.parametrize('field,value', [('start',2.1), ('end',3.2), ('content','changed'), ('index',42)])
def test_changed_cue_invalidates_saved_assignment(tmp_path, field, value):
    segments=cues()
    with patch.object(voice, 'classify_all_segments_gender', return_value=evidence('female')) as classify:
        first=decide(tmp_path/'job',segments)
        unchanged=decide(tmp_path/'job',segments)
        assert unchanged['cue_hash']==first['cue_hash']
        assert classify.call_count==1
        segments[1][field]=value
        updated=decide(tmp_path/'job',segments)
        assert classify.call_count==2
        assert updated['cue_hash']!=first['cue_hash']


def test_duplicate_cues_rejected_before_reusing_snapshot(tmp_path):
    segments=cues()
    with patch.object(voice, 'classify_all_segments_gender', return_value=evidence('female')):
        decide(tmp_path/'job',segments)
    segments[1]['index']=1
    with pytest.raises(ValueError,match='Duplicate'):
        decide(tmp_path/'job',segments)
