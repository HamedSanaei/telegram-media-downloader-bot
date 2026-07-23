from yt_dlp_plugins.extractor.example_public_media import ExamplePublicMediaIE


def test_template_extractor_matches_only_its_owned_domain() -> None:
    assert ExamplePublicMediaIE.suitable("https://media.example.org/items/demo-1")
    assert not ExamplePublicMediaIE.suitable("https://example.com/items/demo-1")
