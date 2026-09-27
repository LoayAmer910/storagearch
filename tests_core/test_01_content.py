"""Step 1 - content detection. The kind comes from the bytes only (I2), every kind in the corpus is
recognized, text that starts like a binary signature is still text, and no input - random, truncated,
corrupt - makes detection raise."""
import hashlib
import os
import random
import shutil

import pytest

from core import content
from core.content import ContentKind as K
import corpus

NAMES = ("x.txt", "x.jpg", "x.png", "x.bmp", "x.docx", "x.exe", "x", "שם בעברית.dat", "UPPER.JPEG", "x.tar.gz")


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    return corpus.build(str(tmp_path_factory.mktemp("corpus")))


def test_every_corpus_file_gets_its_kind(samples):
    wrong = {n: content.classify(s.path).kind for n, s in samples.items() if content.classify(s.path).kind is not s.kind}
    assert wrong == {}, {n: (got, samples[n].kind) for n, got in wrong.items()}


def test_same_bytes_under_any_name_give_the_same_result(samples, tmp_path):
    differing = {}
    for name, sample in samples.items():
        reference = content.classify(sample.path)
        for i, other in enumerate(NAMES):
            folder = tmp_path / f"{name}_{i}"
            folder.mkdir()
            copy = folder / other
            shutil.copyfile(sample.path, copy)
            got = content.classify(str(copy))
            if got != reference:
                differing[(name, other)] = (reference, got)
    assert differing == {}, differing


def test_hash_and_size_are_of_the_whole_file(samples):
    for sample in samples.values():
        with open(sample.path, "rb") as f:
            data = f.read()
        info = content.classify(sample.path)
        assert (info.size, info.sha256) == (len(data), hashlib.sha256(data).hexdigest()), sample.name


def test_a_multibyte_character_cut_by_the_sample_boundary_is_still_text(tmp_path):
    for shift in range(4):
        body = ("x" * (content.SAMPLE_BYTES - 1 - shift) + "ש" * 3000).encode("utf-8")
        path = tmp_path / f"cut_{shift}.txt"
        path.write_bytes(body)
        assert content.classify(str(path)).kind is K.TEXT, shift


def test_invalid_utf8_in_the_middle_of_the_sample_is_not_hidden_by_the_boundary_rule():
    body = b"valid text " * 100 + b"\xc3\x28" + b" more text" * 100 + b"\x01\x02\x03\x04\x05" * 20
    assert content.detect(body, len(body) + 10**6)[0] is K.BINARY


def test_binary_with_a_few_control_bytes_is_not_mistaken_for_text():
    rng = random.Random(3)
    body = bytes(rng.choice(b"abcdefgh\x01\x02\x03\x04\x05\x06\x07\x08\x0e\x0f") for _ in range(5000))
    assert content.detect(body, len(body))[0] is K.BINARY


def test_detection_never_raises_on_arbitrary_or_truncated_input():
    rng = random.Random(11)
    signatures = [b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF89a", b"II*\x00", b"MM\x00*", b"RIFF", b"RIFF\x00\x00\x00\x00WEBP",
                  b"\x00\x00\x00\x18ftyp", b"%PDF-", b"SQLite format 3\x00", b"\x1f\x8b", b"PK\x03\x04", b"\x7fELF", b"MZ",
                  b"BM", b"BZh9", b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf"]
    heads = [rng.randbytes(rng.randrange(0, 300)) for _ in range(3000)]
    heads += [sig[:k] for sig in signatures for k in range(len(sig) + 1)]
    heads += [sig + rng.randbytes(rng.randrange(0, 80)) for sig in signatures for _ in range(50)]
    for head in heads:
        kind, _detail = content.detect(head, len(head))
        assert isinstance(kind, K)
        kind, _detail = content.detect(head, len(head) + 10**7)  # the sample of a much larger file
        assert isinstance(kind, K)


def test_bmp_signature_alone_is_not_enough():
    fake = b"BM" + b"\x00" * 8 + (10**9).to_bytes(4, "little") + (40).to_bytes(4, "little") + b"\x10" * 40
    assert content.detect(fake, len(fake))[0] is not K.BMP


def test_mz_with_a_pe_offset_outside_the_sample_is_not_a_pe():
    head = bytearray(b"MZ" + b"\x00" * 62)
    head[0x3C:0x40] = (10**6).to_bytes(4, "little")
    kind, detail = content.detect(bytes(head), 10**7)
    assert detail != "pe"


def test_unreadable_paths_raise_oserror(tmp_path):
    with pytest.raises(OSError):
        content.classify(str(tmp_path / "missing.bin"))
    with pytest.raises(OSError):
        content.classify(str(tmp_path))


def test_a_zip_whose_central_directory_is_damaged_is_still_a_zip(tmp_path):
    data = bytearray(corpus.ooxml("word/document.xml"))
    data[-22:] = b"\x00" * 22  # wipe the end-of-central-directory record
    path = tmp_path / "damaged.docx"
    path.write_bytes(bytes(data))
    info = content.classify(str(path))
    assert info.kind is K.ZIP and info.detail == "unreadable"


def test_the_classifier_never_opens_the_name(monkeypatch, samples):
    # os.path functions on the name would be the only way to peek at an extension
    for fn in ("splitext", "basename"):
        monkeypatch.setattr(os.path, fn, lambda *_a, **_k: (_ for _ in ()).throw(AssertionError(f"os.path.{fn} used")))
    for sample in samples.values():
        content.classify(sample.path)
