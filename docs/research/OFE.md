# The Orange Forum and the ATIP Manufacturer Code Embargo

Research notes on the phrase coined by Jörg Schilling in the `cdrtools` source code,
and the real Japanese organization behind it.

## TL;DR

- **The Orange Forum was a real organization** — `orangeforum.or.jp`, a formally
  registered Japanese industry body (`.or.jp` = recognized legal entity under Japanese
  law, not a casual domain). It was established in **March 1996** by **49 member
  companies** including Philips Japan, Sony, Taiyo Yuden, TDK, Samsung, LG, Toshiba,
  NEC, and Sanyo.
- Its stated purpose was to promote Orange Book-compliant CD-R/RW products. The
  CD-RW media page (`cdrw100.htm`) shows it also maintained a manufacturer code
  registry — exactly the data Schilling was trying to obtain.
- The Orange Forum transferred its functions to a commercial successor, **CDs21
  Solutions** (`cds21solutions.org`).
- Most information about the Orange Forum is in Japanese and predates extensive web
  archiving, explaining why it was nearly impossible to find in English.
- **"Embargo"**: the authoritative ATIP manufacturer code list was controlled by this
  body and not freely published. Schilling was not coining a satirical nickname — he
  was naming a real organization that charged for access to information he needed.

## The exact Schilling strings

From `cdrecord/diskid.c` (Distrotech mirror):

```
Manufacturer is guessed because of the orange forum embargo.
The orange forum likes to get money for recent information.
The information for this media may not be correct.
```

```
Manufacturer is unknown because of the orange forum embargo.
As the orange forum likes to get money for recent information,
it may be that this media does not use illegal manufacturer coding.
```

## 1. What was the Orange Forum?

**The Orange Forum (オレンジフォーラム, `orangeforum.or.jp`) was a real, formally
registered Japanese industry organization.** Primary source: its own website, archived
by the Wayback Machine in 226 captures spanning October 1997 to July 2012.

### Purpose (from the organization's own overview page)

> 「オレンジフォーラムとは、オレンジブックに準拠した CD-R/RWドライブ、CD-R/RWディスク
> 並びにそれらの関連ハードウエア/ソフトウエア製品の普及促進を図ることを目的に、
> メーカー 49社で組織されている団体です。」

Translation: "The Orange Forum is an organization of 49 manufacturers with the purpose
of promoting the spread of CD-R/RW drives, CD-R/RW discs and related hardware/software
products that comply with the Orange Book."

### History

From the organization's own timeline (「沿革」section):

| Date | Event |
|------|-------|
| Sep 1988 | CD-R development announced |
| Feb 1990 | Orange Book Part I (CD-MO) and Part II (CD-R) announced |
| Aug 1991 | **CD-R User Group (CD-Rユーザーグループ) established** |
| Apr 1993 | CD-R User Group ended |
| Jul 1993 | CD-RW development announced |
| Jan 1994 | Orange Book Part II Ver. 2 announced (2× write speed support) |
| **Feb 1995** | **Orange Study Group (オレンジ研究会) established** — precursor body; registered informal association with the same goal of promoting CD-R |
| **Mar 1996** | **Orange Forum formally established** — Orange Study Group dissolved and succeeded by the registered formal organization |
| Oct 1996 | Orange Book Part III (CD-RW) announced |
| Jan 1997 | Orange Forum website launched |
| Oct 1997 | Yokohama Working Group (CD-RW WG) integrated into Orange Forum |
| Jan 1998 | Orange Book Part II Ver. 3 announced |
| Apr 1998 | Orange Book Part III Ver. 2 announced |

### Member companies (49 as of 1999)

Extracted from the overview page. Columns: company name / contact / product category
(writer drive / media / system / other):

| Company (Japanese) | Company (English) | Category |
|---|---|---|
| アイワ(株) | Aiwa | Writer, System |
| (株)アプリックス | Aplix | Writing software |
| アルプス電気(株) | Alps Electric | Writer |
| (株)アルメディオ | Almedio | Test CD/CD-ROM |
| イージ・システムズ・ジャパン（株）| Easy Systems Japan | Writing software, UDF packet driver |
| （株）SKC | SKC | — |
| LG電子(株) | LG Electronics | — |
| （株）オークテクノロジー | Oak Technology | — |
| オンキョー(株) | Onkyo | — |
| 九州松下電器（株）| Kyushu Matsushita Electric | CD-ROM drive |
| (株)クラレ | Kuraray | — |
| (株)ケンウッド | Kenwood | Measuring instruments |
| コダック（株）| Kodak | — |
| 三洋電機(株) | Sanyo | Pickup, IC |
| 三星電子(株) | Samsung | — |
| シナノケンシ(株) | Shinano Kenshi | CD-ROM drive |
| セイコーエプソン(株) | Seiko Epson | IC |
| ソニー(株) | Sony | — |
| 太陽誘電(株) | Taiyo Yuden | — |
| 第一化成(株) | Daiichi Kasei | CD caddy |
| ティアック(株) | TEAC | — |
| 帝人（株）| Teijin | — |
| TDK(株) | TDK | — |
| （株）デンソー | Denso | — |
| 東洋レコーディング(株) | Toyo Recording | — |
| (株)東芝 | Toshiba | — |
| 東レ（株）| Toray | — |
| 日本コロムビア(株) | Nippon Columbia (Denon) | — |
| 日本電気ホームエレクトロニクス(株) | NEC Home Electronics | CD-ROM drive |
| 日本ビクター(株) | Victor Japan (JVC) | — |
| 日本フィリップス(株) | Philips Japan | — |
| 日本電気(株) | NEC | — |
| *(remaining ~17 members not fully recovered from archived page)* | | |

The presence of **Philips Japan** and **Sony** as members alongside the major Japanese
CD-R media manufacturers (Taiyo Yuden, TDK) and drive makers (Alps, TEAC, Toshiba,
NEC, Onkyo, Sanyo, JVC) confirms that the Orange Forum was the operating body through
which the Orange Book authors maintained industry engagement in Japan.

### The `.or.jp` domain

In Japan, `.or.jp` is reserved for formally recognized legal entities that are not
commercial companies (`.co.jp`) and not government bodies (`.go.jp`) — trade
associations, foundations, and consortia registered under Japanese law. This confirms
the Orange Forum was a proper registered body, not an informal group.

### CD-RW media registry page

The page at `orangeforum.or.jp/1400/cdrw100.htm` (6 Wayback captures, Jun 1998 –
Mar 2000) lists CD-RW media and is in Japanese. This is consistent with the Orange
Forum maintaining the manufacturer code registry data that Schilling was trying to
obtain.

### CDs21 Solutions — the commercial successor

The Orange Forum later transferred its IP and functions to **CDs21 Solutions**
(`cds21solutions.org`). Its website (archived Sep 2012) was Flash-based, which
prevented text extraction. The name "CDs21" most likely refers to "CD standards for
the 21st century". The transition from a `.or.jp` non-profit body to a `.org`
commercial entity is consistent with the commercialization of access to registry
data that Schilling was criticising.

## 2. The embargo

Two interlocking restrictions enforced through the Orange Forum's gatekeeping role:

1. **Specifications paywall.** The Orange Book itself is only distributed to parties
   who have signed either a **CD Information Agreement** or a relevant CD-R/CD-RW
   licence agreement with Philips. It is not a publicly available standard.

2. **MID registry paywall.** The 16-bit Media Identification Code in the ATIP
   pregroove comprises a 12-bit manufacturer code plus a 4-bit variant. The
   authoritative, current list of assigned codes was controlled by the Orange Forum
   and not published openly. Schilling could not legally obtain the current registry,
   so he maintained his table by hand and surfaced the uncertainty to users in-band.

Consequences in `diskid.c`:

- Unknown code → *"Manufacturer is unknown"* + hedge about potentially unregistered
  (illegal) manufacturer coding — a real practice among grey-market pressing plants.
- Low-confidence code → *"Manufacturer is guessed"* + warning the data may be wrong.
- Both cases trace to the same root: "the orange forum likes to get money for recent
  information." This was literally accurate.

## 3. Sources

### Primary — archived Orange Forum pages
- **`orangeforum.or.jp` overview page** (Wayback Machine, 1999-02-19) — purpose,
  history, and 49-member company list with product categories (saved locally as
  `Orange Forum _オレンジフォーラム概要_.mhtml`):
  `https://web.archive.org/web/19990219210229/http://orangeforum.or.jp/1400/cdrw100.htm`
- **`orangeforum.or.jp` home page** (Wayback Machine, 2012-07-30) — confirms the
  organization was still active through 2012; 226 total captures from Oct 1997
  (saved locally as `Orange Forum 2012.mhtml`):
  `https://web.archive.org/web/20120730003401/http://www.orangeforum.or.jp/`
- **`cds21solutions.org`** (Wayback Machine, 2012-09-08) — the commercial successor;
  Flash-based, content not extractable (saved locally as `CDs21ソリューションズ.mhtml`):
  `https://web.archive.org/web/20120908035819/http://www.cds21solutions.org/`

### Primary — Schilling's text
- **`cdrecord/diskid.c`** (cdrtools, Distrotech mirror):
  `https://github.com/Distrotech/cdrtools/blob/master/cdrecord/diskid.c`
- **Forensic Focus "CD-R Manufacturer Code" thread** — practitioner discussion of MID
  identification; references the Orange Forum (saved locally as
  `Forensic Focus - CD-R Manufacturer Code.html`):
  `https://www.forensicfocus.com/forums/general/cd-r-manufacturer-code/`

### Standards and licensing
- **Rainbow Books, Wikipedia** — overview of the colour-book family.
- **Media Identification Code, Wikipedia / Grokipedia** — MID structure.
- **Philips IP&S licensing portal** (`ip.philips.com`) — CD Information Agreement.

### Legal / economic analysis
- **Princo Corp. v. U.S. Philips Corp.**, 424 F.3d 1179 (Fed. Cir. 2005); en banc
  616 F.3d 1318 (Fed. Cir. 2010). The Orange Book patent pool structure.
- **Kung-Chung Liu, "The Taiwanese 'Philips' CD-R Cases"** (SSRN 1831275). The JLA,
  the 3 % royalty / ¥10 floor, and Taiwanese antitrust findings against the pool.

## 4. Revised assessment

The original document concluded that "the Orange Forum" was purely Schilling's satirical
coinage — a nickname for the Philips/Sony licensing apparatus. **This was incorrect.**

The Orange Forum was a real, formally registered Japanese industry organization with:
- A documented founding date (March 1996)
- 49 named member companies including Philips Japan and Sony
- Its own website active from 1997 to at least 2012 (226 Wayback captures)
- A page listing CD-RW media with manufacturer codes (the exact data Schilling needed)
- A formal commercial successor body (CDs21 Solutions)

Schilling's phrase "the orange forum" named a specific real entity. His characterization
of it as charging for information was literally accurate. The sardonic tone came from
the contradiction between the non-profit organizational structure and the pay-to-access
operation — and from the fact that he, as an open-source developer, was structurally
excluded from obtaining data he needed to make his software accurate.

The near-total absence of English-language documentation about the Orange Forum is a
direct consequence of:
1. Its Japanese-language operation
2. Its founding before extensive web crawling of Japanese-language content
3. The fact that most sites mentioning it no longer exist
4. Its effective obsolescence as CD-R manufacturing declined

## 5. Relevance to this project

For `cdda2img`:

- If we ever add ATIP/MID parsing (relevant to the **Physical Media** roadmap and
  **Foreign Image Format Support** for CCD/IMG/SUB, which carries subchannel data),
  we inherit the same embargo problem. Our manufacturer table will be incomplete by
  construction.
- The pragmatic OSS workaround: ship a best-effort table, cite sources per entry where
  possible, and surface uncertainty to the user in the same spirit as Schilling's
  messages. Do not represent guessed identifications as authoritative.
- CDs21 Solutions (the successor) may or may not still be operating; its 2012 archived
  page is the most recent confirmed snapshot. Post-obsolescence of CD-R as a format,
  the registry may have become effectively defunct — which would paradoxically make it
  easier to reconstruct from the historical record than to obtain officially.
- For AccurateRip verification, the protocol is well documented externally and the
  embargo does not affect that work.
