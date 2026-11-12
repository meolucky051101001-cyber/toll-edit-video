import pytest
from video_utils import _srt_style


@pytest.mark.parametrize('color,primary,outline', [
    ('&H00000000', '&H00000000', '&H00FFFFFF'),
    ('&H00FFFFFF', '&H00FFFFFF', '&H00000000'),
    ('#FF0000', '&H000000FF', '&H00000000'),
    ('&H112233&', '&H00112233', '&H00FFFFFF'),
    ('bad,value', '&H00000000', '&H00FFFFFF'),
])
def test_srt_keeps_text_color_and_contrasting_outline(color, primary, outline):
    style = _srt_style('Arial', color, 2)
    assert 'PrimaryColour=' + primary in style
    assert 'OutlineColour=' + outline in style


def test_font_cannot_inject_extra_style_fields():
    style = _srt_style("Arial,Outline=20:'", '#000000', 1)
    assert ',Outline=20' not in style
    assert ',Outline=0.7,' in style
    assert 'Bold=0,' in style
