from src.claude.facade import augment_prompt_with_images


def test_augment_prompt_lists_image_paths(tmp_path):
    p = tmp_path / "shot.png"
    out = augment_prompt_with_images("describe this", [p])
    assert "describe this" in out
    assert str(p) in out
    assert "Read" in out  # nudge to use the Read tool


def test_augment_prompt_no_images_unchanged():
    assert augment_prompt_with_images("hello", None) == "hello"
