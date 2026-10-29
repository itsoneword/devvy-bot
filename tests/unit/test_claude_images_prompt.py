from src.claude.facade import augment_prompt_with_images


def test_augment_prompt_lists_image_paths(tmp_path):
    p = tmp_path / "shot.png"
    out = augment_prompt_with_images("describe this", [p])
    assert "describe this" in out
    assert str(p) in out
    # dv-275f: Read of a raster file is denied; view_image is the way to look.
    assert "view_image" in out
    assert "Read tool" not in out


def test_augment_prompt_no_images_unchanged():
    assert augment_prompt_with_images("hello", None) == "hello"
