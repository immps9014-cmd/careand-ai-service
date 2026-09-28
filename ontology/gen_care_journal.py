#!/usr/bin/env python
"""
care-journal 계층 생성 — care_journal.json → care-journal.ttl (사업계획서 기능 37·40)

  실행: ./venv/bin/python ontology/gen_care_journal.py   (load.sh 가 스키마 적재 전에 부른다)

원본은 JSON 한 벌이다. 이 서버엔 rdflib 이 없어서, ai-service 가 Fuseki 장애 때 같은 칩 목록을
쓰려면(기능 42 무중단) 파이썬이 바로 읽을 수 있는 원본이 필요했다. TTL 은 여기서만 만든다 — 손으로 고치지 말 것.

  cj:Chip        칩(55) — cj:code(앱·DB 저장값) · rdfs:label · cj:inCategory · cj:hasPhrase · cj:tone · cj:order
  cj:Phrase      표현조각(55) — cj:text (보호자에게 보이는 문장 조각)
  cj:ChipCategory 분류(11)
  cj:raisesAlert → care:SafetyAlert  (위험 칩 10 ↔ 안전 알림 10종, 기능 41 결정적 알림)
  cj:observes    → care:CareTerm     (케어용어와 같은 관찰이면 연결)
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "care_journal.json")
OUT = os.path.join(HERE, "care-journal.ttl")


def lit(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main() -> None:
    d = json.load(open(SRC, encoding="utf-8"))
    L = [
        "# 자동 생성 — gen_care_journal.py (원본 care_journal.json). 직접 고치지 말 것.",
        "@prefix care: <http://caren.aiclaude.kr/ontology#> .",
        "@prefix cj:   <http://caren.aiclaude.kr/ontology/journal#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix owl:  <http://www.w3.org/2002/07/owl#> .",
        "@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .",
        "",
        'cj:Chip a owl:Class ; rdfs:label "케어일지 칩"@ko ; rdfs:comment "돌봄전문가가 눌러 기록하는 관찰·수행 단위(기능 40)."@ko .',
        'cj:Phrase a owl:Class ; rdfs:label "표현조각"@ko ; rdfs:comment "칩 하나에 대응하는 보호자용 문장 조각. LLM 은 이것만 이어 붙여 일지를 만든다."@ko .',
        'cj:ChipCategory a owl:Class ; rdfs:label "칩 분류"@ko .',
        'cj:code a owl:DatatypeProperty ; rdfs:label "칩 코드(저장값)"@ko ; rdfs:domain cj:Chip .',
        'cj:inCategory a owl:ObjectProperty ; rdfs:domain cj:Chip ; rdfs:range cj:ChipCategory .',
        'cj:hasPhrase a owl:ObjectProperty ; rdfs:domain cj:Chip ; rdfs:range cj:Phrase .',
        'cj:text a owl:DatatypeProperty ; rdfs:domain cj:Phrase .',
        'cj:tone a owl:DatatypeProperty ; rdfs:label "어조(good·neutral·caution·alert)"@ko .',
        'cj:order a owl:DatatypeProperty .',
        'cj:raisesAlert a owl:ObjectProperty ; rdfs:label "안전 알림 유발"@ko ; rdfs:domain cj:Chip ; rdfs:range care:SafetyAlert .',
        'cj:observes a owl:ObjectProperty ; rdfs:label "관찰 케어용어"@ko ; rdfs:domain cj:Chip ; rdfs:range care:CareTerm .',
        "",
    ]
    for c in d["categories"]:
        L.append(f'cj:Cat_{c["id"]} a cj:ChipCategory ; rdfs:label {lit(c["label"])}@ko ; cj:order {c["order"]} .')
    L.append("")
    for i, ch in enumerate(d["chips"], 1):
        parts = [
            f'cj:Chip_{ch["id"]} a cj:Chip',
            f'cj:code {lit(ch["id"])}',
            f'rdfs:label {lit(ch["label"])}@ko',
            f'cj:inCategory cj:Cat_{ch["cat"]}',
            f'cj:hasPhrase cj:Phrase_{ch["id"]}',
            f'cj:tone {lit(ch["tone"])}',
            f"cj:order {i}",
        ]
        if ch.get("alert"):
            parts.append(f'cj:raisesAlert care:{ch["alert"]}')
        if ch.get("term"):
            parts.append(f'cj:observes care:{ch["term"]}')
        L.append(" ;\n    ".join(parts) + " .")
        L.append(f'cj:Phrase_{ch["id"]} a cj:Phrase ; cj:text {lit(ch["phrase"])}@ko .')
    open(OUT, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print(f"care-journal.ttl: 칩 {len(d['chips'])} · 표현조각 {len(d['chips'])} · 분류 {len(d['categories'])}")


if __name__ == "__main__":
    main()
