from app.textnorm import cn_to_int, normalize, parse_mmss, to_simplified


def test_traditional_input_becomes_simplified():
    assert to_simplified("絕命毒師") == "绝命毒师"


def test_normalize_strips_evasion_and_markup():
    assert normalize("《摩登-家庭 第五季》") == "摩登家庭第五季"
    assert normalize('<em class="keyword">摩登家庭</em> 全11季') == "摩登家庭全11季"
    assert normalize("Breaking Bad") == "breakingbad"


def test_cn_to_int():
    assert [cn_to_int(x) for x in ["五", "十", "十一", "二十四", "11", "abc"]] == [5, 10, 11, 24, 11, None]


def test_parse_mmss():
    assert parse_mmss("8135:14") == 488114
    assert parse_mmss("18:0") == 1080
