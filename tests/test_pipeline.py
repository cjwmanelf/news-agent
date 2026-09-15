"""파이프라인 단위 테스트.

실행: .venv/Scripts/python.exe -m unittest discover -s tests -v

네트워크를 타지 않는 순수 로직만 검증한다.
수집기·발행기의 실제 통신은 run.py --stage 로 따로 확인한다.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from newsagent.collectors.api import dig  # noqa: E402
from newsagent.llm import MockLLM, extract_json  # noqa: E402
from newsagent.models import (  # noqa: E402
    DISPUTED,
    LIKELY,
    SINGLE_SOURCE,
    VERIFIED,
    Article,
    Brief,
    FactCheck,
    VerifiedBrief,
)
from newsagent.nodes.collect import deduplicate  # noqa: E402
from newsagent.nodes.curate import _excluded, _keyword_score, keyword_idf  # noqa: E402
from newsagent.nodes.verify import (  # noqa: E402
    dedupe_sources,
    decide_verdict,
    rank_candidates,
    score_confidence,
    source_key,
)
from newsagent.publishers.discord import build_embed, chunk_embeds  # noqa: E402
from newsagent.search import build_query  # noqa: E402
from newsagent.store import PublishedStore  # noqa: E402
from newsagent.utils.text import (  # noqa: E402
    clean_text,
    domain_of,
    event_similarity_to,
    make_id,
    normalize_url,
    parse_datetime,
    title_key,
    wire_origin,
)


def article(
    title: str, *, url: str, snippet: str = "", domain: str | None = None,
    content: str = "", published_at: str | None = None,
) -> Article:
    return Article(
        id=make_id(url),
        title=title,
        url=url,
        source=domain or domain_of(url),
        domain=domain or domain_of(url),
        source_type="rss",
        snippet=snippet,
        content=content,
        published_at=published_at,
    )


def find_peers(target, candidates, *, cluster_threshold=0.35, duplicate_threshold=0.9,
               max_peers=6, max_age_hours=0):
    """verify_one 의 증인 확보 흐름(순위 → 독립성 판정)을 테스트용으로 합친 것."""
    ranked = rank_candidates(
        target, candidates,
        cluster_threshold=cluster_threshold,
        max_age_hours=max_age_hours,
        pool_size=max_peers * 3,
    )
    peers, _ = dedupe_sources(
        target, ranked, duplicate_threshold=duplicate_threshold, max_peers=max_peers
    )
    return peers


class TestTextUtils(unittest.TestCase):
    def test_normalize_url_strips_tracking(self):
        a = normalize_url("https://www.example.com/news/1?utm_source=x&id=7&fbclid=z")
        b = normalize_url("http://example.com/news/1/?id=7")
        self.assertEqual(a.split("://")[1], b.split("://")[1])

    def test_clean_text_unescapes_entities(self):
        # 피드 description 에 흔한 &nbsp; 가 그대로 새어나가면 요약에 박힌다
        self.assertEqual(clean_text("<p>삼성&nbsp;&nbsp;전자</p>"), "삼성 전자")
        self.assertEqual(clean_text("AT&amp;T &lt;b&gt;굵게&lt;/b&gt;"), "AT&T 굵게")

    def test_title_key_ignores_punctuation(self):
        self.assertEqual(
            title_key("삼성전자·SK하이닉스, '전기료' 거절"),
            title_key("삼성전자 SK하이닉스 전기료 거절"),
        )

    def test_parse_datetime_formats(self):
        self.assertIsNotNone(parse_datetime("2026-09-14T10:00:00Z"))
        self.assertIsNotNone(parse_datetime("Mon, 14 Sep 2026 10:00:00 +0900"))
        self.assertIsNotNone(parse_datetime(1789357749))
        self.assertIsNone(parse_datetime("어제쯤?"))  # 실패는 None → 호출부가 기사 유지

    def test_event_similarity_separates_same_event_from_same_company(self):
        """4단계의 생명선. 같은 기업이 나온다고 같은 사건이 아니다."""
        target = "삼성전자·SK하이닉스, 한전 '전기료 25조 선납' 제안 거절"
        same_event = "삼성전자·SK하이닉스, '25조 전기료 선납' 한전 제안 거절"
        same_company = "삼성전자·SK하이닉스 증설 본격화 반도체 팹 인프라 수주 확대 전망"
        unrelated = "오픈AI 에이전트 루비젬스 해킹 시도 논란"

        sims = event_similarity_to(target, [same_event, same_company, unrelated])
        self.assertGreater(sims[0], 0.5, "같은 사건은 확실히 높아야 한다")
        self.assertLess(sims[1], 0.35, "같은 기업·다른 사건은 임계값 아래여야 한다")
        self.assertGreater(sims[0] - sims[1], 0.25, "변별폭이 충분해야 한다")
        self.assertLess(sims[2], 0.1)


class TestCollect(unittest.TestCase):
    def test_deduplicate_by_url_and_same_domain_title(self):
        items = [
            article("같은 기사", url="https://a.com/1"),
            article("같은 기사", url="https://a.com/1?utm_source=rss"),  # URL 정규화로 중복
            article("같은 기사!", url="https://a.com/2"),  # 같은 매체 제목 중복
            article("다른 기사", url="https://a.com/3"),
        ]
        unique, dropped = deduplicate(items)
        self.assertEqual(len(unique), 2)
        self.assertEqual(dropped, 2)

    def test_deduplicate_keeps_same_title_from_different_outlets(self):
        """매체가 다른데 제목이 같은 건 중복이 아니라 교차검증 증인이다."""
        items = [
            article("삼성전자 HBM 공급 확대", url="https://a.com/1"),
            article("삼성전자 HBM 공급 확대", url="https://b.com/1"),
            article("삼성전자 HBM 공급 확대", url="https://c.com/1"),
        ]
        unique, dropped = deduplicate(items)
        self.assertEqual(len(unique), 3)
        self.assertEqual(dropped, 0)

    def test_dig_dotted_path(self):
        payload = {"data": {"articles": [{"source": {"name": "연합뉴스"}}]}}
        self.assertEqual(dig(payload, "data.articles.0.source.name"), "연합뉴스")
        self.assertIsNone(dig(payload, "data.missing.name"))


class TestCurate(unittest.TestCase):
    def setUp(self):
        # 'AI' 는 4건 전부에, 'HBM' 은 1건에만 등장 → IDF 가 크게 갈려야 한다
        self.corpus = [
            article("AI 교육 프로그램 운영", url="https://a.com/1"),
            article("AI 세미나 개최", url="https://b.com/2"),
            article("AI 도입 확대", url="https://c.com/3"),
            article("HBM 공급 계약 체결, AI 수요 대응", url="https://d.com/4"),
        ]
        self.interest = {"name": "반도체", "keywords": ["AI", "HBM"], "exclude": ["루머"]}

    def test_idf_downweights_ubiquitous_keywords(self):
        idf = keyword_idf(self.corpus, [self.interest])
        self.assertLess(idf["ai"], idf["hbm"])
        self.assertLess(idf["ai"], 0.3)

    def test_rare_keyword_outscores_common_one(self):
        idf = keyword_idf(self.corpus, [self.interest])
        common, _ = _keyword_score(self.corpus[0], self.interest, idf)  # AI 만 매칭
        rare, _ = _keyword_score(self.corpus[3], self.interest, idf)  # HBM + AI
        self.assertGreater(rare, common)

    def test_exclude_is_hard_filter(self):
        rumor = article("HBM 신제품 루머 확산", url="https://e.com/5")
        self.assertTrue(_excluded(rumor, self.interest))
        self.assertFalse(_excluded(self.corpus[3], self.interest))


class TestVerifyScoring(unittest.TestCase):
    def test_confidence_rewards_independent_sources(self):
        alone = score_confidence(1, 0, 0, 3)
        three = score_confidence(3, 3, 0, 3)
        self.assertLess(alone, 0.25)
        self.assertGreater(three, 0.9)

    def test_contradiction_penalty(self):
        clean = score_confidence(3, 3, 0, 3)
        disputed = score_confidence(3, 2, 1, 3)
        self.assertGreater(clean - disputed, 0.4)

    def test_verdicts(self):
        self.assertEqual(decide_verdict(3, 0.9, 1), DISPUTED, "상충이 있으면 출처가 많아도 DISPUTED")
        self.assertEqual(decide_verdict(3, 0.8, 0), VERIFIED)
        self.assertEqual(decide_verdict(2, 0.6, 0), LIKELY)
        self.assertEqual(decide_verdict(1, 0.2, 0), SINGLE_SOURCE)
        self.assertEqual(decide_verdict(3, 0.5, 0), LIKELY, "출처 3곳이어도 신뢰도 0.7 미만이면 VERIFIED 불가")
        self.assertEqual(decide_verdict(3, 0.45, 0), SINGLE_SOURCE, "출처가 많아도 신뢰도가 낮으면 승격 금지")
        self.assertEqual(decide_verdict(1, 0.95, 0), SINGLE_SOURCE, "출처가 하나면 지지율이 높아도 승격 금지")


class TestSelectPeers(unittest.TestCase):
    def setUp(self):
        self.target = article(
            "삼성전자·SK하이닉스, 한전 '전기료 25조 선납' 제안 거절", url="https://inews24.com/1"
        )

    def test_same_domain_is_not_corroboration(self):
        peers = find_peers(
            self.target,
            [article("삼성전자·SK하이닉스, '25조 전기료 선납' 한전 제안 거절", url="https://inews24.com/2")],
            cluster_threshold=0.35, duplicate_threshold=0.9, max_peers=6,
        )
        self.assertEqual(peers, [], "같은 매체의 재송고는 증인이 아니다")

    def test_different_domain_same_event_counts(self):
        peers = find_peers(
            self.target,
            [article("삼성전자·SK하이닉스, '25조 전기료 선납' 한전 제안 거절", url="https://yna.co.kr/2")],
            cluster_threshold=0.35, duplicate_threshold=0.9, max_peers=6,
        )
        self.assertEqual([p.domain for p, _ in peers], ["yna.co.kr"])

    def test_unrelated_article_is_rejected(self):
        peers = find_peers(
            self.target,
            [article("삼성전자·SK하이닉스 증설 본격화 반도체 팹 인프라 수주 확대", url="https://ebn.co.kr/2")],
            cluster_threshold=0.35, duplicate_threshold=0.9, max_peers=6,
        )
        self.assertEqual(peers, [], "같은 기업이 나올 뿐 다른 사건이면 증인이 아니다")

    def test_syndicated_reprint_counted_once(self):
        """통신사 기사를 그대로 전재한 매체들을 독립 출처로 세면 신뢰도가 부풀려진다."""
        headline = "삼성전자·SK하이닉스, 한전 '전기료 25조 선납' 제안 거절"
        peers = find_peers(
            self.target,
            [
                article(headline, url="https://a-news.com/2"),
                article(headline, url="https://b-news.com/3"),  # 글자까지 동일한 전재
                article(headline, url="https://c-news.com/4"),
            ],
            cluster_threshold=0.35, duplicate_threshold=0.9, max_peers=6,
        )
        self.assertEqual(len(peers), 1)

    def test_max_peers_is_respected(self):
        headline = "삼성전자·SK하이닉스, 한전 전기료 25조 선납 제안 거절"
        candidates = [
            article(f"{headline} {i}번째 보도 상세", url=f"https://outlet{i}.com/x")
            for i in range(10)
        ]
        peers = find_peers(
            self.target, candidates,
            cluster_threshold=0.35, duplicate_threshold=0.99, max_peers=3,
        )
        self.assertLessEqual(len(peers), 3)


class TestWireService(unittest.TestCase):
    """통신사 전재를 독립 출처로 세면 신뢰도가 부풀려진다."""

    def test_wire_origin_patterns(self):
        self.assertEqual(wire_origin("(서울=연합뉴스) 김철수 기자 = 삼성전자가"), "연합뉴스")
        self.assertEqual(wire_origin("[세종=뉴시스] 이영희 기자 ="), "뉴시스")
        self.assertEqual(wire_origin("본문. 저작권자 ⓒ 연합뉴스 무단전재 금지"), "연합뉴스")
        self.assertEqual(wire_origin("WASHINGTON (Reuters) - The company"), "Reuters")
        self.assertIsNone(wire_origin("[아이뉴스24 권서아 기자] 삼성전자와"))
        self.assertIsNone(wire_origin(""))

    def test_source_key_prefers_wire_over_domain(self):
        own = article("제목", url="https://inews24.com/1", content="[아이뉴스24 기자] 내용")
        reprint = article("제목", url="https://kookje.co.kr/1", content="(서울=연합뉴스) 기자 = 내용")
        self.assertEqual(source_key(own), "inews24.com")
        self.assertEqual(source_key(reprint), "연합뉴스")

    # 매체마다 제목을 조금씩 고쳐 실은 전재본.
    # 쌍별 유사도가 0.58~0.87 이라 근접중복 임계값(0.9)으로는 걸러지지 않는다.
    # 즉 아래 두 테스트는 통신사 탐지 경로만을 검증한다.
    REPRINT_TITLES = [
        "삼성전자 SK하이닉스 한전 25조 전기료 선납 제안 거절",
        "삼성전자와 SK하이닉스 한전 전기료 선납 25조 제안 수용 불가",
        "한전 25조 전기료 선납 제안 삼성전자 SK하이닉스 모두 거절",
    ]

    def _target(self):
        return article("삼성전자 SK하이닉스 한전 전기료 25조 선납 제안 거절",
                       url="https://inews24.com/1", content="[아이뉴스24 기자] 자체 취재 내용")

    def test_wire_reprints_from_many_outlets_count_once(self):
        # 도메인은 셋이지만 원 취재원은 연합뉴스 하나다
        candidates = [
            article(title, url=f"https://outlet{i}.co.kr/1",
                    content=f"(서울=연합뉴스) 김기자 = {title} 관련 세부 내용이 이어진다.")
            for i, title in enumerate(self.REPRINT_TITLES)
        ]
        peers = find_peers(self._target(), candidates)
        self.assertEqual(len(peers), 1, "통신사 전재 3건은 독립 출처 1곳이다")
        self.assertEqual(source_key(peers[0][0]), "연합뉴스")

    def test_same_articles_without_wire_byline_count_separately(self):
        """대조군 — 바이라인이 없으면 같은 3건이 독립 출처 3곳으로 잡힌다.

        위 테스트가 근접중복 필터가 아니라 통신사 탐지 덕에 통과한다는 증거다.
        """
        candidates = [
            article(title, url=f"https://outlet{i}.co.kr/1",
                    content=f"[아웃렛{i} 자체 취재] {title} 관련 세부 내용이 이어진다.")
            for i, title in enumerate(self.REPRINT_TITLES)
        ]
        peers = find_peers(self._target(), candidates)
        self.assertEqual(len(peers), 3)


class TestRelaySources(unittest.TestCase):
    """링크가 중계 URL 인 소스(finnhub 등)는 도메인이 전부 같아 매체를 구분할 수 없다."""

    def make(self, publisher, url):
        a = article("삼성전자 HBM 공급 확대", url=url)
        a.publisher = publisher
        return a

    def test_publisher_used_when_domain_is_a_relay(self):
        yahoo = self.make("Yahoo", "https://finnhub.io/api/news?id=aaa")
        fool = self.make("Fool", "https://finnhub.io/api/news?id=bbb")
        self.assertEqual(source_key(yahoo), "yahoo")
        self.assertEqual(source_key(fool), "fool")
        self.assertNotEqual(source_key(yahoo), source_key(fool))

    def test_domain_wins_for_normal_sources(self):
        normal = self.make("아이뉴스24", "https://inews24.com/view/1")
        self.assertEqual(source_key(normal), "inews24.com")

    def test_wire_still_beats_publisher(self):
        reprint = self.make("Yahoo", "https://finnhub.io/api/news?id=ccc")
        reprint.content = "(서울=연합뉴스) 김기자 = 내용"
        self.assertEqual(source_key(reprint), "연합뉴스")

    def test_relay_articles_count_as_separate_witnesses(self):
        """같은 중계 호스트에서 왔어도 매체가 다르면 독립 출처 둘로 센다.

        기사 문구는 서로 다르게 둔다. 똑같이 두면 근접중복 필터가 먼저 걸러버려서
        publisher 로직이 실제로 동작하는지 검증하지 못한다.
        """
        target = article("삼성전자 SK하이닉스 한전 전기료 25조 선납 제안 거절",
                         url="https://inews24.com/1")
        peers = find_peers(target, [
            self.make_event("Yahoo", "aaa", "삼성전자 SK하이닉스 한전 25조 전기료 선납 제안 거절"),
            self.make_event("Fool", "bbb", "한전 25조 전기료 선납 제안 삼성전자 SK하이닉스 모두 거절"),
        ])
        self.assertEqual(sorted(source_key(p) for p, _ in peers), ["fool", "yahoo"])

    def make_event(self, publisher, ident, title):
        a = article(title, url=f"https://finnhub.io/api/news?id={ident}")
        a.publisher = publisher
        return a


class TestApiDatePlaceholders(unittest.TestCase):
    """기간을 요구하는 API 는 날짜를 고정으로 적으면 하루 만에 낡는다."""

    def test_expand_dates(self):
        from datetime import datetime, timedelta, timezone

        from newsagent.collectors.api import expand_dates

        now = datetime.now(timezone.utc)
        self.assertEqual(expand_dates("{today}"), now.strftime("%Y-%m-%d"))
        self.assertEqual(expand_dates("{yesterday}"), (now - timedelta(days=1)).strftime("%Y-%m-%d"))
        self.assertEqual(expand_dates("{days_ago:7}"), (now - timedelta(days=7)).strftime("%Y-%m-%d"))
        self.assertEqual(expand_dates("from={today}&x=1"), f"from={now.strftime('%Y-%m-%d')}&x=1")
        self.assertEqual(expand_dates("변화없음"), "변화없음")
        self.assertEqual(expand_dates(30), 30)

    def test_unix_timestamp_strings(self):
        """finnhub 의 datetime 은 Unix 초를 문자열로 준다."""
        parsed = parse_datetime("1789357441")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed, parse_datetime(1789357441))
        self.assertEqual(parse_datetime("1789357441000"), parsed, "밀리초도 같은 시각")


class TestPeerRecency(unittest.TestCase):
    """오래된 유사 기사가 증인으로 잡히면 교차검증이 조용히 오염된다."""

    def setUp(self):
        self.target = article(
            "삼성전자 SK하이닉스 한전 전기료 25조 선납 제안 거절",
            url="https://inews24.com/1", published_at="2026-09-14T10:00:00+00:00",
        )
        self.fresh = article(
            "삼성전자 SK하이닉스 한전 전기료 25조 선납 제안 거절 확인",
            url="https://yna.co.kr/1", published_at="2026-09-14T12:00:00+00:00",
        )
        self.stale = article(
            "삼성전자 SK하이닉스 한전 전기료 25조 선납 제안 거절 보도",
            url="https://kookje.co.kr/1", published_at="2025-01-05T09:00:00+00:00",
        )

    def test_stale_peer_is_rejected(self):
        peers = find_peers(self.target, [self.fresh, self.stale], max_age_hours=96)
        domains = [p.domain for p, _ in peers]
        self.assertIn("yna.co.kr", domains)
        self.assertNotIn("kookje.co.kr", domains, "1년 전 기사는 증인이 될 수 없다")

    def test_age_check_disabled_keeps_everything(self):
        peers = find_peers(self.target, [self.fresh, self.stale], max_age_hours=0)
        self.assertEqual(len(peers), 2)

    def test_unknown_date_is_kept(self):
        """발행시각을 모른다는 이유로 증인을 버리지는 않는다 (수집 단계와 같은 원칙)."""
        undated = article(
            "삼성전자 SK하이닉스 한전 전기료 25조 선납 제안 거절 속보",
            url="https://news1.kr/1",
        )
        peers = find_peers(self.target, [undated], max_age_hours=96)
        self.assertEqual(len(peers), 1)


class TestPublishedStore(unittest.TestCase):
    """같은 소식이 며칠 연속 나가는 것을 막는다."""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.store = PublishedStore(Path(self.tmp.name) / "published.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _rows(self):
        return [{
            "article_id": "abc123", "url": "https://a.com/1", "headline": "삼성전자 HBM 공급 확대",
            "event_text": "삼성전자 HBM 공급 확대 계약 체결", "domain": "a.com",
            "verdict": VERIFIED, "confidence": 0.8,
        }]

    def test_record_then_recall(self):
        self.assertEqual(self.store.record(self._rows(), "run1"), 1)
        self.assertIn("abc123", self.store.published_ids(7))
        events = self.store.recent_events(7)
        self.assertEqual(len(events), 1)
        self.assertIn("HBM", events[0][0])

    def test_record_is_idempotent(self):
        self.store.record(self._rows(), "run1")
        self.assertEqual(self.store.record(self._rows(), "run2"), 0, "같은 기사를 두 번 세지 않는다")
        self.assertEqual(len(self.store.published_ids(7)), 1)

    def test_prune_removes_old_rows(self):
        self.store.record(self._rows(), "run1")
        self.assertEqual(self.store.prune(keep_days=0), 1)
        self.assertEqual(self.store.published_ids(7), set())

    def test_disabled_store_is_harmless(self):
        disabled = PublishedStore(None, enabled=False)
        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.published_ids(7), set())
        self.assertEqual(disabled.recent_events(7), [])
        self.assertEqual(disabled.record(self._rows(), "run"), 0)
        disabled.close()


class TestLLMUsage(unittest.TestCase):
    def test_mock_tracks_calls(self):
        llm = MockLLM()
        llm.summarize(source="s", title="t", published_at="", content="문장 하나가 여기에 있습니다.")
        llm.fact_check(source="s", headline="h", facts=["f"], peers=[])
        self.assertEqual(llm.usage["calls"], 2)
        self.assertEqual(llm.usage["failures"], 0)


class TestLLM(unittest.TestCase):
    def test_extract_json_from_fenced_and_noisy_output(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```')["a"], 1)
        self.assertEqual(extract_json('설명입니다.\n{"a": {"b": 2}}\n끝.')["a"]["b"], 2)
        self.assertEqual(extract_json('{"text": "중괄호 } 포함"}')["text"], "중괄호 } 포함")
        with self.assertRaises(ValueError):
            extract_json("JSON 아님")

    def test_mock_llm_contract(self):
        """mock 백엔드도 3·4단계가 기대하는 키를 반드시 채워야 한다."""
        payload = MockLLM().summarize(
            source="테스트", title="제목", published_at="", content="첫 번째 문장입니다. 두 번째 문장입니다."
        )
        for key in ("headline", "summary", "key_facts", "entities", "category"):
            self.assertIn(key, payload)

        checked = MockLLM().fact_check(
            source="테스트", headline="제목", facts=["삼성전자가 HBM 공급을 확대한다"],
            peers=[{"source": "연합", "title": "삼성전자 HBM 공급 확대", "text": ""}],
        )
        self.assertEqual(len(checked["fact_checks"]), 1)
        self.assertIn(checked["fact_checks"][0]["status"], ("supported", "contradicted", "unverified"))


class TestDiscordPublisher(unittest.TestCase):
    def make_item(self, verdict=VERIFIED, summary_len=1) -> VerifiedBrief:
        art = article("제목", url="https://a.com/1")
        brief = Brief(
            article_id=art.id, headline="헤드라인", summary=["요약"] * summary_len,
            key_facts=["사실"], entities={}, category="기술",
            why_it_matters="중요", article=art,
        )
        return VerifiedBrief(
            brief=brief, verdict=verdict, confidence=0.8,
            corroborating_sources=["b.com", "c.com"], cluster_size=3,
            fact_checks=[FactCheck(fact="사실", status="supported", evidence="b.com")],
        )

    def test_embed_respects_discord_limits(self):
        embed = build_embed(self.make_item(summary_len=400))
        self.assertLessEqual(len(embed["title"]), 256)
        self.assertLessEqual(len(embed["description"]), 4096)
        for field in embed["fields"]:
            self.assertLessEqual(len(field["value"]), 1024)

    def test_chunk_respects_embed_count(self):
        embeds = [build_embed(self.make_item()) for _ in range(23)]
        chunks = chunk_embeds(embeds, 10)
        self.assertTrue(all(len(c) <= 10 for c in chunks))
        self.assertEqual(sum(len(c) for c in chunks), 23)

    def test_chunk_respects_total_chars(self):
        """embed 합계 6000자를 넘기면 Discord 가 요청 전체를 400 으로 거절한다."""
        embeds = [build_embed(self.make_item(summary_len=300)) for _ in range(6)]
        chunks = chunk_embeds(embeds, 10)
        for chunk in chunks:
            total = sum(
                len(e["title"]) + len(e["description"]) + len(e["footer"]["text"])
                + sum(len(f["name"]) + len(f["value"]) for f in e["fields"])
                for e in chunk
            )
            self.assertLessEqual(total, 6000)

    def test_disputed_gets_warning_color(self):
        self.assertNotEqual(
            build_embed(self.make_item(DISPUTED))["color"],
            build_embed(self.make_item(VERIFIED))["color"],
        )


class TestDiscordSendFlow(unittest.TestCase):
    """실제 웹후크 없이 전송 순서·재시도·페이로드 형태를 검증한다."""

    def setUp(self):
        art = article("제목", url="https://a.com/1")
        brief = Brief(
            article_id=art.id, headline="헤드라인", summary=["요약"],
            key_facts=["사실"], entities={}, category="기술", why_it_matters="", article=art,
        )
        self.items = [
            VerifiedBrief(brief=brief, verdict=VERIFIED, confidence=0.8,
                          corroborating_sources=["b.com"], cluster_size=2, fact_checks=[])
            for _ in range(12)
        ]

    def _publisher(self, responses):
        from unittest import mock

        from newsagent.publishers.discord import DiscordPublisher

        publisher = DiscordPublisher("https://discord.test/webhook", {"send_interval": 0})
        posted = []

        def fake_post(url, json=None, timeout=None):
            posted.append(json)
            return responses.pop(0)

        # requests 는 이제 공용 재시도 헬퍼(base)에서 쓴다
        return publisher, posted, mock.patch("newsagent.publishers.base.requests.post", fake_post)

    @staticmethod
    def _response(status, payload=None):
        from unittest import mock

        response = mock.Mock()
        response.status_code = status
        response.json.return_value = payload or {}
        response.text = ""
        response.headers = {}
        return response

    def test_header_then_chunked_embeds(self):
        publisher, posted, patcher = self._publisher([self._response(204) for _ in range(3)])
        with patcher:
            result = publisher.publish(self.items, {"title": "T", "date": "2026-09-14", "stats": {}})
        self.assertEqual(len(posted), 3, "헤더 1건 + embed 묶음 2건")
        self.assertIn("content", posted[0])
        self.assertEqual(len(posted[1]["embeds"]), 10)
        self.assertEqual(len(posted[2]["embeds"]), 2)
        self.assertEqual(result["embeds_sent"], 12)

    def test_rate_limit_is_retried(self):
        responses = [self._response(429, {"retry_after": 0.01}), self._response(204),
                     self._response(204), self._response(204)]
        publisher, posted, patcher = self._publisher(responses)
        with patcher:
            publisher.publish(self.items[:1], {"title": "T", "date": "d", "stats": {}})
        self.assertEqual(len(posted), 3, "429 로 막힌 헤더를 한 번 더 보내고 embed 를 보낸다")

    def test_http_error_raises(self):
        publisher, _, patcher = self._publisher([self._response(400)])
        with patcher, self.assertRaises(RuntimeError):
            publisher.publish(self.items[:1], {"title": "T", "date": "d", "stats": {}})

    def test_server_error_is_retried(self):
        """규격에 맞는 페이로드인데도 Discord 가 500 을 준 적이 있다.

        일시적 서버 오류를 한 번에 포기하면 그날 뉴스레터가 통째로 날아간다.
        """
        from unittest import mock

        responses = [self._response(500), self._response(204),   # 헤더: 한 번 실패 후 성공
                     self._response(204)]                         # embed 묶음
        publisher, posted, patcher = self._publisher(responses)
        with patcher, mock.patch("newsagent.publishers.base.time.sleep"):
            result = publisher.publish(self.items[:1], {"title": "T", "date": "d", "stats": {}})
        self.assertEqual(len(posted), 3, "500 을 만난 헤더를 다시 보내고 embed 까지 전송")
        self.assertEqual(result["embeds_sent"], 1)

    def test_client_error_is_not_retried(self):
        """4xx 는 우리 잘못이라 다시 보내도 같은 결과다. 즉시 멈춰야 한다."""
        from unittest import mock

        publisher, posted, patcher = self._publisher([self._response(404)] * 4)
        with patcher, mock.patch("newsagent.publishers.base.time.sleep"), self.assertRaises(RuntimeError):
            publisher.publish(self.items[:1], {"title": "T", "date": "d", "stats": {}})
        self.assertEqual(len(posted), 1, "재시도하지 않는다")

    def test_error_message_says_which_message_failed(self):
        from unittest import mock

        publisher, _, patcher = self._publisher([self._response(400)])
        with patcher, mock.patch("newsagent.publishers.base.time.sleep"):
            with self.assertRaises(RuntimeError) as caught:
                publisher.publish(self.items[:1], {"title": "T", "date": "d", "stats": {}})
        self.assertIn("헤더", str(caught.exception))

    def test_network_error_is_retried(self):
        from unittest import mock

        import requests as rq

        from newsagent.publishers.base import post_with_retry

        calls = []

        def flaky(url, json=None, timeout=None):
            calls.append(1)
            if len(calls) < 3:
                raise rq.ConnectionError("일시적 네트워크 오류")
            return self._response(204)

        with mock.patch("newsagent.publishers.base.requests.post", flaky), \
             mock.patch("newsagent.publishers.base.time.sleep"):
            post_with_retry("https://x.test", {}, label="테스트")
        self.assertEqual(len(calls), 3)


class TestSecrets(unittest.TestCase):
    """GUI 에서 API 키를 저장할 때 지켜야 할 두 가지: 값 비노출, 기존 파일 보존."""

    def setUp(self):
        import tempfile

        from newsagent import secrets as sec

        self.sec = sec
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".env"
        self.path.write_text(
            "# 내 메모\nOPENAI_API_KEY=sk-old-value-1234\n\n"
            "# 우리가 모르는 항목\nMY_OWN_SETTING=keep-me\n",
            encoding="utf-8",
        )
        self._saved_env = {k: os.environ.get(k) for k in sec.MANAGED}

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_mask_never_shows_full_secret(self):
        masked = self.sec.mask("OPENAI_API_KEY", "sk-abcdefghijklmnop")
        self.assertNotIn("abcdefghij", masked)
        self.assertTrue(masked.endswith("mnop"))
        self.assertEqual(self.sec.mask("OPENAI_API_KEY", "short"), "•••••")
        # 비밀이 아닌 값은 그대로 보여준다
        self.assertEqual(self.sec.mask("TELEGRAM_CHAT_ID", "-100123"), "-100123")

    def test_update_preserves_comments_and_unknown_keys(self):
        self.sec.update_env({"ANTHROPIC_API_KEY": "sk-ant-new"}, self.path)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# 내 메모", text)
        self.assertIn("MY_OWN_SETTING=keep-me", text)
        self.assertIn("ANTHROPIC_API_KEY=sk-ant-new", text)
        self.assertIn("OPENAI_API_KEY=sk-old-value-1234", text)

    def test_update_replaces_in_place(self):
        self.sec.update_env({"OPENAI_API_KEY": "sk-replaced"}, self.path)
        values = self.sec.read_env(self.path)
        self.assertEqual(values["OPENAI_API_KEY"], "sk-replaced")
        self.assertEqual(self.path.read_text(encoding="utf-8").count("OPENAI_API_KEY"), 1)

    def test_clear_empties_value_and_process_env(self):
        self.sec.update_env({"OPENAI_API_KEY": "sk-x"}, self.path)
        self.assertEqual(os.environ.get("OPENAI_API_KEY"), "sk-x")
        self.sec.update_env({"OPENAI_API_KEY": None}, self.path)
        self.assertEqual(self.sec.read_env(self.path).get("OPENAI_API_KEY"), "")
        self.assertIsNone(os.environ.get("OPENAI_API_KEY"))

    def test_describe_reports_status_without_values(self):
        self.sec.update_env({"OPENAI_API_KEY": "sk-abcdefghijklmnop"}, self.path)
        described = self.sec.describe(self.path)
        self.assertTrue(described["OPENAI_API_KEY"]["set"])
        self.assertFalse(described["SLACK_WEBHOOK_URL"]["set"])
        blob = json.dumps(described, ensure_ascii=False)
        self.assertNotIn("sk-abcdefghijklmnop", blob)

    def test_special_characters_survive_dotenv(self):
        """따옴표·역슬래시·공백이 든 값이 dotenv 로 그대로 되읽히는가.

        escape 를 빠뜨리면 `KEY="ab"cd"` 같은 깨진 줄이 나오고 dotenv 가 그 줄을
        통째로 버린다. GUI 에는 '저장됨' 으로 보이는데 파이프라인은 키 없이 도는,
        가장 알아채기 어려운 실패라 여기서 못박아 둔다.
        """
        from dotenv import dotenv_values

        tricky = {
            "T_QUOTE": 'ab"cd ef',
            "T_BACKSLASH": r"pa\th with space",
            "T_BOTH": r'a"b\c d',
            "T_HASH": "tok#en value",
            "T_NEWLINE": "line1\nline2",
            "T_SPACES": "  padded  ",
            "T_APOSTROPHE": "it's fine",
            "T_PLAIN": "https://discord.com/api/webhooks/1/abcDEF-_",
        }
        self.sec.update_env(dict(tricky), self.path)

        parsed = dotenv_values(self.path)
        mine = self.sec.read_env(self.path)
        for name, value in tricky.items():
            self.assertEqual(parsed.get(name), value, f"dotenv 가 {name} 를 되읽지 못함")
            self.assertEqual(mine.get(name), value, f"read_env 가 {name} 를 되읽지 못함")

        # 기존 주석과 우리가 모르는 항목은 그대로 남아야 한다
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# 내 메모", text)
        self.assertIn("MY_OWN_SETTING=keep-me", text)


class TestPipelineCleanup(unittest.TestCase):
    """Pipeline.close() 가 연 자원을 전부 닫는가.

    GUI 는 프로세스를 띄워둔 채 실행마다 파이프라인을 새로 만든다.
    하나라도 놓치면 실행 횟수만큼 SQLite 커넥션과 파일 핸들이 쌓인다.
    """

    def setUp(self):
        import sqlite3
        import tempfile

        self.sqlite3 = sqlite3
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _alive(self, conn):
        try:
            conn.execute("SELECT 1")
            return True
        except self.sqlite3.ProgrammingError:
            return False

    def test_close_releases_store_and_checkpointer(self):
        from newsagent.graph import Pipeline, build_checkpointer

        saver = build_checkpointer(self.dir / "cp.db")
        store = PublishedStore(self.dir / "pub.db")
        pipeline = Pipeline(graph=None, llm=None, store=store, checkpointer=saver)

        self.assertTrue(self._alive(store._conn))
        self.assertTrue(self._alive(saver.conn))

        pipeline.close()
        self.assertIsNone(store._conn)
        self.assertFalse(self._alive(saver.conn))

        pipeline.close()  # 두 번 불러도 터지지 않아야 한다 (세션 정리 경로가 중복 호출한다)

    def test_close_checkpointer_handles_orphan_and_none(self):
        """build_pipeline 이 도중에 실패하면 Pipeline 없이 체크포인터만 남는다."""
        from newsagent.graph import build_checkpointer, close_checkpointer

        orphan = build_checkpointer(self.dir / "orphan.db")
        close_checkpointer(orphan)
        self.assertFalse(self._alive(orphan.conn))

        close_checkpointer(None)  # 체크포인터를 안 쓰는 실행 경로


class TestPublisherRegistry(unittest.TestCase):
    def setUp(self):
        from newsagent import publishers

        self.publishers = publishers

    def test_configured_targets_accepts_list_and_legacy_single(self):
        self.assertEqual(self.publishers.configured_targets({"targets": ["discord", "slack"]}),
                         ["discord", "slack"])
        self.assertEqual(self.publishers.configured_targets({"target": "telegram"}), ["telegram"])
        self.assertEqual(self.publishers.configured_targets({"targets": "slack"}), ["slack"])
        self.assertEqual(self.publishers.configured_targets({}), [])

    def test_missing_env_lists_required_names(self):
        saved = {k: os.environ.pop(k, None) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
        try:
            self.assertEqual(
                sorted(self.publishers.missing_env("telegram")),
                ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
            )
            os.environ["TELEGRAM_BOT_TOKEN"] = "t"
            self.assertEqual(self.publishers.missing_env("telegram"), ["TELEGRAM_CHAT_ID"])
        finally:
            for key, value in saved.items():
                os.environ.pop(key, None)
                if value is not None:
                    os.environ[key] = value


class TestSlackAndTelegram(unittest.TestCase):
    def make_items(self, count=1, bullets=1):
        items = []
        for i in range(count):
            art = article(f"제목{i}", url=f"https://a{i}.com/1")
            brief = Brief(
                article_id=art.id, headline=f"헤드라인{i}", summary=["요약"] * bullets,
                key_facts=["사실"], entities={}, category="기술",
                why_it_matters="중요", article=art,
            )
            items.append(VerifiedBrief(
                brief=brief, verdict=VERIFIED, confidence=0.8,
                corroborating_sources=["b.com"], cluster_size=2, fact_checks=[],
            ))
        return items

    def test_slack_escapes_mrkdwn_specials(self):
        from newsagent.publishers.slack import mrkdwn_escape

        self.assertEqual(mrkdwn_escape("a & b <c>"), "a &amp; b &lt;c&gt;")

    def test_slack_chunks_below_block_limit(self):
        from newsagent.publishers.slack import MAX_BLOCKS, build_blocks, build_header_blocks

        items = self.make_items(30)
        chunks, current = [], build_header_blocks(items, {"title": "T", "date": "d", "stats": {}})
        for item in items:
            blocks = build_blocks(item)
            if len(current) + len(blocks) > MAX_BLOCKS:
                chunks.append(current)
                current = []
            current.extend(blocks)
        chunks.append(current)
        self.assertTrue(all(len(c) <= MAX_BLOCKS for c in chunks))
        self.assertGreater(len(chunks), 1)

    def test_telegram_escapes_html(self):
        from newsagent.publishers.telegram import esc, render_item

        self.assertEqual(esc("<b>&</b>"), "&lt;b&gt;&amp;&lt;/b&gt;")
        art = article("제목 <script>", url="https://a.com/1")
        brief = Brief(article_id=art.id, headline="위험 <tag> & 기호", summary=["본문 <b>"],
                      key_facts=[], entities={}, category="기술", why_it_matters="", article=art)
        rendered = render_item(VerifiedBrief(brief=brief, verdict=VERIFIED, confidence=0.5))
        self.assertIn("&lt;tag&gt;", rendered)
        self.assertNotIn("<tag>", rendered)

    def test_telegram_respects_message_length(self):
        from newsagent.publishers.telegram import MAX_CHARS, render_header, render_item

        items = self.make_items(20, bullets=40)
        blocks = [render_header(items, {"title": "T", "date": "d", "stats": {}})]
        blocks += [render_item(i) for i in items]
        messages, current = [], ""
        for block in blocks:
            if current and len(current) + len(block) + 2 > MAX_CHARS:
                messages.append(current)
                current = block
            else:
                current = f"{current}\n\n{block}" if current else block
        messages.append(current)
        self.assertTrue(all(len(m) <= MAX_CHARS + 2 for m in messages))
        self.assertGreater(len(messages), 1)


class TestSearchQuery(unittest.TestCase):
    def test_build_query_strips_brackets_and_punctuation(self):
        query = build_query("[AI & LAW] 국내대리인 역할은 무엇?")
        self.assertNotIn("[", query)
        self.assertNotIn("?", query)
        self.assertIn("국내대리인", query)

    def test_short_title_yields_short_query(self):
        self.assertEqual(build_query("!!!"), "")


class TestCollectNodeLookback(unittest.TestCase):
    def test_per_source_lookback_hours_overrides_global(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        from newsagent.nodes.collect import make_collect_node

        now = datetime.now(timezone.utc)
        recent_date = (now - timedelta(hours=10)).isoformat()
        old_date = (now - timedelta(hours=48)).isoformat()

        art_recent = article("최근 기사", url="https://src1.com/1", published_at=recent_date)
        art_old_1 = article("오래된 기사 1", url="https://src1.com/2", published_at=old_date)
        art_old_2 = article("오래된 기사 2", url="https://src2.com/3", published_at=old_date)

        class FakeCollector:
            def __init__(self, spec, cfg):
                self.name = spec["name"]

            def collect(self):
                if self.name == "src1":
                    return [art_recent, art_old_1]
                return [art_old_2]

        cfg = {
            "collect": {
                "lookback_hours": 24,
                "max_workers": 2,
                "timeout": 5,
            },
            "sources": [
                {"name": "src1", "enabled": True, "type": "rss", "url": "https://src1.com/rss"},
                {"name": "src2", "enabled": True, "type": "rss", "url": "https://src2.com/rss", "lookback_hours": 72},
            ],
        }

        with patch("newsagent.nodes.collect.build_collector", side_effect=lambda s, c: FakeCollector(s, c)):
            node = make_collect_node(cfg)
            result = node({})

        collected_titles = {a.title for a in result["raw_items"]}
        self.assertIn("최근 기사", collected_titles)
        self.assertNotIn("오래된 기사 1", collected_titles, "src1은 전역 24h라 48h 전 기사는 탈락")
        self.assertIn("오래된 기사 2", collected_titles, "src2는 lookback_hours: 72라 48h 전 기사도 수집")

    def test_validate_config_rejects_invalid_lookback_hours(self):
        import copy
        from newsagent.config import ConfigError, DEFAULTS, validate_config

        base_cfg = copy.deepcopy(DEFAULTS)
        base_cfg["curate"]["interests"] = [{"name": "AI", "keywords": ["AI"]}]
        base_cfg["sources"] = [
            {"name": "test_src", "type": "rss", "url": "https://a.com", "role": "content", "lookback_hours": -5},
        ]
        with self.assertRaises(ConfigError):
            validate_config(base_cfg)

        base_cfg["sources"][0]["lookback_hours"] = "not_int"
        with self.assertRaises(ConfigError):
            validate_config(base_cfg)

        base_cfg["sources"][0]["lookback_hours"] = 168
        validate_config(base_cfg)

    def test_build_sources_parses_lookback_hours(self):
        from app import build_sources, PatchError

        incoming = [
            {"name": "엔비디아", "type": "rss", "url": "https://nvidianews.nvidia.com/rss.xml", "lookback_hours": "168"},
            {"name": "일반언론", "type": "rss", "url": "https://news.com/rss.xml", "lookback_hours": ""},
        ]
        built = build_sources(incoming, [])
        self.assertEqual(built[0]["lookback_hours"], 168)
        self.assertNotIn("lookback_hours", built[1])

        with self.assertRaises(PatchError):
            build_sources([{"name": "오류", "type": "rss", "url": "https://a.com", "lookback_hours": "-10"}], [])


class TestNewsScheduler(unittest.TestCase):
    def test_interval_mode_next_run(self):
        from datetime import datetime, timedelta
        from newsagent.scheduler import NewsScheduler

        now = datetime(2026, 9, 14, 10, 0, 0)
        sch = NewsScheduler()
        sch.update_config({"enabled": True, "mode": "interval", "interval_hours": 3})
        next_run = sch.compute_next_run(now)
        self.assertEqual(next_run, now + timedelta(hours=3))

    def test_daily_mode_next_run(self):
        from datetime import datetime
        from newsagent.scheduler import NewsScheduler

        sch = NewsScheduler()
        sch.update_config({"enabled": True, "mode": "daily", "daily_time": "08:30"})

        # 시각 전: 오늘 08:30
        now_before = datetime(2026, 9, 14, 7, 0, 0)
        self.assertEqual(sch.compute_next_run(now_before), datetime(2026, 9, 14, 8, 30, 0))

        # 시각 후: 내일 08:30
        now_after = datetime(2026, 9, 14, 9, 0, 0)
        self.assertEqual(sch.compute_next_run(now_after), datetime(2026, 9, 15, 8, 30, 0))

    def test_disabled_scheduler_has_no_next_run(self):
        from newsagent.scheduler import NewsScheduler

        sch = NewsScheduler()
        sch.update_config({"enabled": False})
        self.assertIsNone(sch.compute_next_run())

    def test_validate_config_rejects_invalid_schedule(self):
        import copy
        from newsagent.config import ConfigError, DEFAULTS, validate_config

        cfg = copy.deepcopy(DEFAULTS)
        cfg["sources"] = [{"name": "s", "type": "rss", "url": "http://s.test"}]
        cfg["curate"]["interests"] = [{"name": "AI", "keywords": ["AI"]}]
        cfg["schedule"] = {"enabled": True, "mode": "invalid_mode"}
        with self.assertRaises(ConfigError):
            validate_config(cfg)

        cfg["schedule"] = {"enabled": True, "mode": "interval", "interval_hours": -1}
        with self.assertRaises(ConfigError):
            validate_config(cfg)

        cfg["schedule"] = {"enabled": True, "mode": "daily", "daily_time": "99:99"}
        with self.assertRaises(ConfigError):
            validate_config(cfg)


if __name__ == "__main__":
    unittest.main()
