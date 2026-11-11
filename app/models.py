from dataclasses import dataclass, field


@dataclass
class SearchHit:
    bvid: str
    title: str
    duration_s: int
    play: int
    pubdate: int
    typename: str = ""
    author: str = ""
    cover: str = ""
    desc: str = ""
    tag: str = ""


@dataclass
class Part:
    name: str
    duration_s: int


@dataclass
class SeasonEp:
    bvid: str
    title: str
    duration_s: int
    play: int


@dataclass
class VideoDetail:
    bvid: str
    title: str
    desc: str = ""
    play: int = 0
    pubdate: int = 0
    author: str = ""
    cover: str = ""
    width: int = 0
    height: int = 0
    parts: list[Part] = field(default_factory=list)
    season_id: int | None = None
    season_title: str | None = None
    season_eps: list[SeasonEp] = field(default_factory=list)


@dataclass
class ResultItem:
    key: str
    bvid: str
    url: str
    title: str
    cover: str
    play: int
    pubdate: int
    total_minutes: int
    form: str  # "merged" | "episodic" | "collection" | ""
    form_label: str
    coverage: str
    quality: str
    subtitle: str
    uploader: str
    status: str  # "complete" | "uncertain" | "excluded"
    reason: str
