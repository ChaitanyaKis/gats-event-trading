# Dedupe rules v1: hand check of 50 random pairs (T2.5)

Pairs accepted by `dedupe-v1` on BSE-complete days 2026-09-02 → 2026-09-17,
sampled with `random.seed(7)`, read by Claude Code on 2026-10-03.
Scores: size (+3 KB-exact, +2 MB-rounded, −1 mismatch) + time (+2 ≤ 15 min,
+1 ≤ 2 h) + 3 × topic similarity; accepted at ≥ 4.

**Result: 47 clearly the same disclosure, 3 uncertain, 0 clearly wrong** →
precision between 94% (uncertain counted as errors) and 100%. All three
uncertain pairs are MB-rounded size matches with near-zero text overlap.

| # | Verdict | Note |
|---|---|---|
| 1 | same |  |
| 2 | same |  |
| 3 | same |  |
| 4 | same |  |
| 5 | same |  |
| 6 | same |  |
| 7 | same |  |
| 8 | same |  |
| 9 | same |  |
| 10 | same |  |
| 11 | same |  |
| 12 | same |  |
| 13 | same |  |
| 14 | same |  |
| 15 | same |  |
| 16 | same |  |
| 17 | same |  |
| 18 | same |  |
| 19 | same |  |
| 20 | same |  |
| 21 | same |  |
| 22 | same |  |
| 23 | same |  |
| 24 | same |  |
| 25 | same |  |
| 26 | uncertain | NSE: Reg 44(3) voting results; BSE: 'shareholders approved final dividend'. Same AGM outcome, maybe the same PDF. |
| 27 | same |  |
| 28 | uncertain | NSE: AGM notice; BSE: newspaper ad of the AGM notice. Possibly different documents. |
| 29 | same |  |
| 30 | same |  |
| 31 | same |  |
| 32 | same |  |
| 33 | same |  |
| 34 | same |  |
| 35 | same |  |
| 36 | same |  |
| 37 | same |  |
| 38 | same |  |
| 39 | same |  |
| 40 | same |  |
| 41 | same |  |
| 42 | same |  |
| 43 | same |  |
| 44 | same |  |
| 45 | same |  |
| 46 | same |  |
| 47 | same |  |
| 48 | same |  |
| 49 | same |  |
| 50 | uncertain | NSE: 'Shareholders meeting'; BSE: Annual Report. Companies often file the annual report under NSE's AGM category. |

## The pairs

```
#1 score=6.00 dt=181s size=True (9531556 vs 9530545) sim=0.67
   N 2026-09-05 07:32:31.000000 | Copy of Newspaper Publication | Uma Exports Limited has informed the Exchange about Copy of Newspaper Publication
   B 2026-09-05 07:29:30.177000 | Company Update / Newspaper Publication | Uma Exports Ltd - 543513 - Announcement under Regulation 30 (LODR)-Newspaper Publication | Newspaper Advertisement.
#2 score=7.00 dt=458s size=True (688200 vs 688197) sim=0.67
   N 2026-09-07 10:26:49.000000 | Copy of Newspaper Publication | Xtranet Technologies Limited has informed the Exchange about Copy of Newspaper Publication
   B 2026-09-07 10:19:10.613000 | Company Update / Newspaper Publication | Xtranet Technologies Ltd - 544838 - Announcement under Regulation 30 (LODR)-Newspaper Publication | Intimation of Newspaper Publication pursuant to the provisions of Regulation 30 of SEBI (Listing Obligations a
#3 score=5.67 dt=787s size=True (617892 vs 617889) sim=0.22
   N 2026-09-04 07:42:27.000000 | Shareholders meeting | Indo-National Limited has informed the Exchange regarding Notice of Annual General Meeting to be held on September 28, 2026
   B 2026-09-04 07:29:19.630000 | AGM/EGM / AGM | Indo National Ltd - 504058 - Notice Of 53Rd Annual General Meeting | The 53rd AGM is scheduled to be held on Monday, September 28, 2026 at 03.00 PM (IST) through Video Conference 
#4 score=7.67 dt=373s size=True (272957 vs 272959) sim=0.89
   N 2026-09-10 11:17:34.000000 | Amalgamation/Merger | The Phoenix Mills Limited has informed the Exchange about effective date of scheme of merger and amalgamation between subsidiary companies.
   B 2026-09-10 11:11:20.557000 | Company Update / Scheme of Arrangement | The Phoenix Mills Ltd - 503100 - Announcement under Regulation 30 (LODR)-Scheme of Arrangement | Intimation of effective date of scheme of merger and amalgamation between subsidiary companies.
#5 score=7.33 dt=195s size=True (285768 vs 285772) sim=0.78
   N 2026-09-12 11:01:46.000000 | Record Date | TAKE Limited has informed the Exchange regarding Intimation of Cut-off Date for E-voting for the 25th Annual General Meeting.
   B 2026-09-12 11:05:00.953000 | Corp. Action / Record Date | Take Ltd - 532890 - Intimation Of Cut-Off Date For E-Voting For The 25Th Annual General Meeting | Intimation of Cut-off Date for E-voting for the 25th Annual General Meeting of the Company.
#6 score=5.00 dt=762s size=True (1090519 vs 1094989) sim=0.33
   N 2026-09-08 17:11:19.000000 | Record Date | Belrise Industries Limited has informed the Exchange that Record date for the purpose of DI is 15-Sep-2026.
   B 2026-09-08 16:58:36.730000 | Corp. Action / Record Date | Belrise Industries Ltd - 544405 - Dividend - Record Date - Tuesday September 15, 2026 | Intimation of Record Date i.e Tuesday September 15, 2026 for the purpose of Dividend.
#7 score=7.14 dt=339s size=True (363848 vs 363846) sim=0.71
   N 2026-09-02 13:51:50.000000 | Analysts/Institutional Investor Meet/Con. Call Updates | Metro Brands Limited has informed the Exchange about outcome of Investor meet
   B 2026-09-02 13:46:10.910000 | Company Update / Analyst / Investor Meet | Metro Brands Ltd - 543426 - Announcement under Regulation 30 (LODR)-Analyst / Investor Meet - Outcome | Outcome of Analyst/Institutional Investor meet.
#8 score=5.82 dt=281s size=True (796600 vs 796603) sim=0.27
   N 2026-09-09 14:30:18.000000 | Shareholders meeting | Equitas Small Finance Bank Limited has informed the Exchange regarding Proceedings of Annual General Meeting held on September 09, 2026
   B 2026-09-09 14:34:58.750000 | AGM/EGM / AGM | Equitas Small Finance Bank Ltd - 543243 - Shareholder Meeting / Postal Ballot-Outcome of AGM | Equitas Small Finance Bank Limited has informed the Exchanges regarding the proceedings of the 10th AGM
#9 score=4.64 dt=288s size=True (8409580 vs 8410483) sim=0.21
   N 2026-09-07 12:08:03.000000 | Outcome of Board Meeting | Shiprocket Limited has submitted to the Exchange, the financial results for the period ended Jun 30, 2026.
   B 2026-09-07 12:12:51.077000 | Result / Financial Results | Shiprocket Ltd - 544871 - Result Of Financial Result For 06.30.2026 | Unaudited Standalone and Consolidated Financial Statement for the Quater ended June 30, 2026
#10 score=7.50 dt=25s size=True (659804 vs 659806) sim=0.83
   N 2026-09-10 02:21:19.000000 | Press Release | Kolte - Patil Developers Limited has informed the Exchange regarding a press release dated September 10, 2026, titled "Kolte-Patil Developers Marks it
   B 2026-09-10 02:21:44.393000 | Company Update / Press Release / Media Release | Kolte-Patil Developers Ltd - 532924 - Announcement under Regulation 30 (LODR)-Press Release / Media Release | Press Release - Kolte-Patil Developers marks its strongest-ever Launch performance with sales over Rs. 600 cro
#11 score=5.00 dt=100s size=True (581786 vs 581784) sim=0.00
   N 2026-09-07 10:23:00.000000 | General Updates | Garuda Construction and Engineering Limited has informed the Exchange about General Updates
   B 2026-09-07 10:21:19.570000 | Company Update / General | Garuda Construction and Engineering Ltd - 544271 - Intimation Regarding Signing Of Memorandum Of Understanding | The Company informed the stock exchanges about MOU between Dream City Builders with Almasarat Company Limited.
#12 score=6.50 dt=38s size=True (220426 vs 220426) sim=0.50
   N 2026-09-10 12:13:37.000000 | Analysts/Institutional Investor Meet/Con. Call Updates | Gland Pharma Limited has informed the Exchange about Schedule of meet
   B 2026-09-10 12:14:15.017000 | Company Update / Analyst / Investor Meet | Gland Pharma Ltd - 543245 - Announcement under Regulation 30 (LODR)-Analyst / Investor Meet - Intimation | Schedule of Investor Meetings
#13 score=8.00 dt=474s size=True (228731 vs 228729) sim=1.00
   N 2026-09-03 01:49:56.000000 | General Updates | Lloyds Enterprises Limited has informed the Exchange about Change in Name of Geomysore Services India Pvt Ltd, Strategic Investment Unit of Lloyds Ent
   B 2026-09-03 01:42:02.303000 | Company Update / General | Lloyds Enterprises Ltd - 512463 - Intimation Of Change In Name Of Geomysore Services India Pvt Ltd, Strategic  | Intimation of Change in Name of Geomysore Services India Pvt Ltd, Strategic Investment Unit of Lloyds Enterpri
#14 score=7.25 dt=760s size=True (610068 vs 610064) sim=0.75
   N 2026-09-03 15:03:58.000000 | Updates | DOMS Industries Limited has informed the Exchange regarding Managing Director Speech Delivered at 20th Annual General Meeting of the Company
   B 2026-09-03 14:51:17.543000 | AGM/EGM / AGM | DOMS Industries Ltd - 544045 - Managing Director Speech Delivered At 20Th Annual General Meeting Of The Compan | Managing Director Speech Delivered at 20th Annual General Meeting
#15 score=5.46 dt=290s size=True (671631 vs 671635) sim=0.15
   N 2026-09-08 11:14:05.000000 | Copy of Newspaper Publication | Tata Elxsi Limited has informed the Exchange about Copy of Newspaper Publication
   B 2026-09-08 11:18:54.930000 | Company Update / Newspaper Publication | Tata Elxsi Ltd - 500408 - Announcement under Regulation 30 (LODR)-Newspaper Publication | Please find enclosed the newspaper advertisement published today i.e., September 08, 2026 regarding opening of
#16 score=5.50 dt=252s size=True (1394606 vs 1395742) sim=0.50
   N 2026-09-04 09:52:12.000000 | Outcome of Board Meeting | Aqylon Nexus Limited has informed the Exchange regarding Outcome of Board Meeting held on September 04, 2026.
   B 2026-09-04 09:56:23.560000 | Others / Outcome without intimation | Aqylon Nexus Ltd - 530943 - Board Meeting Outcome for Outcome Of Board Meeting Held Today I.E.., Friday Septem | Outcom of Board Meeting held today i.e.. Friday, September 4, 2026
#17 score=7.40 dt=448s size=True (750162 vs 750166) sim=0.80
   N 2026-09-02 12:51:53.000000 | Outcome of Board Meeting | Onelife Capital Advisors Limited has informed the Exchange regarding Outcome of Board Meeting held on September 02, 2026.
   B 2026-09-02 12:44:25.070000 | Board Meeting / Outcome of Board Meeting | Onelife Capital Advisors Ltd - 533632 - Board Meeting Outcome for Outcome Of The Board Meeting Of Onelife Capi | Enclosed herewith Outcome of Board Meeting dated 02.09.2026
#18 score=6.40 dt=400s size=True (1405092 vs 1409949) sim=0.80
   N 2026-09-10 04:39:17.000000 | ESOP/ESOS/ESPS | L&T Technology Services Limited has informed the Exchange regarding Allotment of 8,725 Shares.
   B 2026-09-10 04:32:37.140000 | Company Update / Allotment of ESOP / ESPS | L&T Technology Services Ltd - 540115 - Announcement under Regulation 30 (LODR)-Allotment of ESOP / ESPS | Intimation attached for allotment of 8,725 shares.
#19 score=7.14 dt=77s size=True (175053 vs 175050) sim=0.71
   N 2026-09-03 10:42:39.000000 | Analysts/Institutional Investor Meet/Con. Call Updates | INDO-MIM Limited has informed the Exchange about Schedule of meet
   B 2026-09-03 10:41:21.983000 | Company Update / Analyst / Investor Meet | INDO-MIM Ltd - 544837 - Announcement under Regulation 30 (LODR)-Analyst / Investor Meet - Intimation | Intimation of Schedule of Analyst/Institutional Investors Meet under Regulation 30 of the SEBI (Listing Obliga
#20 score=4.86 dt=229s size=True (5494538 vs 5490201) sim=0.29
   N 2026-09-06 16:23:05.000000 | Copy of Newspaper Publication | B&B Triplewall Containers Limited has informed the Exchange about Copy of Newspaper Publication
   B 2026-09-06 16:19:15.607000 | Company Update / Newspaper Publication | B&B Triplewall Containers Ltd - 543668 - Announcement under Regulation 30 (LODR)-Newspaper Publication | Newspaper Publication for completion of dispatch of the notice of 15th Annual General Meeting of the Company.
#21 score=5.50 dt=520s size=True (1793065 vs 1797440) sim=0.50
   N 2026-09-02 10:40:58.000000 | Outcome of Board Meeting | Tree House Education & Accessories Limited has informed the Exchange regarding Outcome of Board Meeting held on September 02, 2026.
   B 2026-09-02 10:32:18.307000 | Others / Outcome without intimation | Tree House Education & Accessories Ltd - 533540 - Board Meeting Outcome for Outcome Of Board Meeting Held Toda | Outcome of Board Meeting held today i.e. 02.09.2026
#22 score=7.50 dt=153s size=True (526121 vs 526119) sim=0.83
   N 2026-09-10 11:20:25.000000 | Press Release | Orient Technologies Limited has informed the Exchange regarding a press release dated September 10, 2026, titled "Orient Technologies Expands Cybersec
   B 2026-09-10 11:17:52.040000 | Company Update / Press Release / Media Release | Orient Technologies Ltd - 544235 - Announcement under Regulation 30 (LODR)-Press Release / Media Release | Press Release- Orient Technologies expands Cybersecurity Business with USD 3.25 Million, Three year Securonix 
#23 score=4.75 dt=206s size=True (1415578 vs 1411130) sim=0.25
   N 2026-09-04 07:14:48.000000 | Shareholders meeting | Sumeet Industries Limited has informed the Exchange regarding Notice of Annual General Meeting to be held on September 29, 2026
   B 2026-09-04 07:18:14.337000 | AGM/EGM / AGM | Sumeet Industries Ltd-$ - 514211 - Notice Of Annual General Meeting For The Year 2025-26 | Notice of Annual General Meeting , Book Closure and Record Date for the year 2025-26
#24 score=7.00 dt=167s size=True (363377 vs 363381) sim=0.67
   N 2026-09-03 13:28:44.000000 | Updates | Dev Information Technology Limited has informed the Exchange regarding 'Intimation Regarding Record Date of Dividend and E-voting.
   B 2026-09-03 13:25:57.420000 | Company Update / General | Dev Information Technology Ltd - 543462 - Announcement Regarding Record Date For The Perouse Of Dividend And E | Announcement Regarding Record Date for The Purpose Of the Dividend and E-voting
#25 score=6.50 dt=232s size=True (218368 vs 218372) sim=0.50
   N 2026-09-05 08:48:49.000000 | Shareholders meeting | Anlon Healthcare Limited has informed the Exchange regarding Proceedings of Annual General Meeting held on September 05, 2026
   B 2026-09-05 08:52:41.000000 | AGM/EGM / AGM | Anlon Healthcare Ltd - 544497 - Shareholder Meeting / Postal Ballot-Outcome of AGM | Please find attached herewith Proceedings of Annual General Meeting held on September 05, 2026
#26 score=4.11 dt=674s size=True (5085594 vs 5083302) sim=0.04
   N 2026-09-12 11:59:40.000000 | Shareholders meeting | Pursuant to the provisions of Regulation 30 and Regulation 44(3) of the SEBI (Listing Obligations and Disclosure Requirements) Regulations, 2015, we h
   B 2026-09-12 11:48:25.703000 | Corp Action / Dividend Updates | Clean Science and Technology Ltd - 543318 - Announcement under Regulation 30 (LODR)-Dividend Updates | Enclosed is the shareholder approved the final dividend of Rs. 4 per share for FY26
#27 score=4.20 dt=2639s size=True (3261071 vs 3260842) sim=0.40
   N 2026-09-05 11:17:33.000000 | Shareholders meeting | The Company has informed the Exchange regarding Notice of Annual General Meeting to be held on September 28, 2026
   B 2026-09-05 10:33:34.120000 | AGM/EGM / AGM | PNC Media and Entertainment Ltd - 532387 - Notice Of 33Rd AGM Scheduled On September 28, 2026 | Notice of 33rd Annual General Meeting for the Financial Year 2025-26 is attached
#28 score=4.64 dt=349s size=True (2170552 vs 2172766) sim=0.21
   N 2026-09-07 06:03:53.000000 | Shareholders meeting | Ahluwalia Contracts (India) Limited has informed the Exchange regarding Notice of Annual General Meeting to be held on September 29, 2026
   B 2026-09-07 05:58:03.883000 | AGM/EGM / AGM | Ahluwalia Contracts (India) Ltd - 532811 - Sub: Compliance With Regulation 47 Of The SEBI (LODR), Regulations  | 47th AGM Notice Published with FE and Jansatta on - 06-09-2026
#29 score=7.45 dt=10s size=True (608123 vs 608120) sim=0.82
   N 2026-09-11 07:56:07.000000 | Press Release | Shilpa Medicare Limited has informed the Exchange regarding a press release dated September 11, 2026, titled "Shilpa Medicare's Novel Complex Injectab
   B 2026-09-11 07:55:57.113000 | Company Update / Press Release / Media Release | Shilpa Medicare Ltd - 530549 - Announcement under Regulation 30 (LODR)-Press Release / Media Release | Shilpa Medicare''s Novel Complex Injectable OERIS (Ondansetron Extended - Release Injection, 100 mg/mL) receiv
#30 score=4.67 dt=37s size=True (11848909 vs 11851574) sim=0.22
   N 2026-09-03 07:36:14.000000 | Copy of Newspaper Publication | Saatvik Green Energy Limited has informed the Exchange about Copy of Newspaper Publication
   B 2026-09-03 07:35:36.653000 | Company Update / Newspaper Publication | Saatvik Green Energy Ltd - 544526 - Announcement under Regulation 30 (LODR)-Newspaper Publication | Newspaper Advertisement for Completion of Dispatch of the Notice of 11th AGM of the company and E-voting infor
#31 score=4.60 dt=64s size=True (2506097 vs 2507354) sim=0.20
   N 2026-09-04 15:38:50.000000 | Copy of Newspaper Publication | Hemisphere Properties India Limited has informed the Exchange about Copy of Newspaper Publication
   B 2026-09-04 15:39:54.157000 | Company Update / Newspaper Publication | Hemisphere Properties India Ltd - 543242 - Announcement under Regulation 30 (LODR)-Newspaper Publication | In compliance with Section 108 of the Companies Act, 2013 read with Rule 20 of Companies (Management and Admin
#32 score=5.64 dt=798s size=True (242790 vs 242788) sim=0.21
   N 2026-09-08 07:59:54.000000 | ESOP/ESOS/ESPS | Tracxn Technologies Limited has informed the Exchange regarding Allotment of 30048 securities pursuant to ESOP/ESPS at its meeting held on September 0
   B 2026-09-08 07:46:35.760000 | Company Update / Allotment of ESOP / ESPS | Tracxn Technologies Ltd - 543638 - Announcement under Regulation 30 (LODR)-Allotment of ESOP / ESPS | Intimation for Allotment of 30,048 Equity Shares under "TRACXN Employee Stock Option Plan 2016"
#33 score=7.62 dt=32s size=True (714260 vs 714265) sim=0.88
   N 2026-09-03 13:26:17.000000 | Outcome of Board Meeting | Outcome of the meeting of the Board of Directors of Mangalam Worldwide Limited ( the Company ) held on today i.e. on Thursday, September 03, 2026.
   B 2026-09-03 13:25:44.717000 | Others / Outcome without intimation | Mangalam Worldwide Ltd - 544764 - Board Meeting Outcome for Outcome Of The Meeting Of The Board Of Directors O | Outcome of Board Meeting held today i.e. on Thursday, September 03, 2026.
#34 score=6.50 dt=191s size=True (497306 vs 497305) sim=0.50
   N 2026-09-08 11:07:45.000000 | General Updates | TITAGARH RAIL SYSTEMS LIMITED has informed the Exchange about Communication to Shareholders w.r.t. Tax Deduction at Source on Dividend Payout
   B 2026-09-08 11:04:33.577000 | Company Update / General | Titagarh Rail Systems Ltd - 532966 - Communication To Shareholders W.R.T. Tax Deduction At Source On Dividend  | A communication was sent to the shareholders w.r.t. Tax Deduction at Source along with dispatch of Notice of A
#35 score=7.68 dt=117s size=True (437391 vs 437394) sim=0.89
   N 2026-09-05 11:05:22.000000 | Press Release | Anupam Rasayan India Limited has informed the Exchange regarding a press release dated September 05, 2026, titled "Anupam Rasayan Secures Chemical Sup
   B 2026-09-05 11:03:24.520000 | Company Update / Press Release / Media Release | Anupam Rasayan India Ltd - 543275 - Announcement under Regulation 30 (LODR)-Press Release / Media Release | Intimation of Press Release titled "Anupam Rasayan secures chemical supply contract with a Global Industrial M
#36 score=4.82 dt=101s size=True (7644119 vs 7648963) sim=0.27
   N 2026-09-08 12:10:49.000000 | Copy of Newspaper Publication | AVRO INDIA LIMITED has informed the Exchange about Copy of Newspaper Publication w.r.t pre dispatch of Notice of 30th AGM
   B 2026-09-08 12:09:08.423000 | Company Update / Newspaper Publication | Avro India Ltd - 543512 - Announcement under Regulation 30 (LODR)-Newspaper Publication | Pursuant to Reg. 30 & 47 of SEBI (Listing Obligations and Disclosure Requirements) Regulations, 2015, please f
#37 score=7.00 dt=237s size=True (1593836 vs 1594846) sim=1.00
   N 2026-09-02 13:45:05.000000 | General Updates | Gayatri Highways Limited has informed the Exchange about Completion of Sale of stake in the Associate Company pursuant to the Securities Purchase Agre
   B 2026-09-02 13:49:02.033000 | Company Update / General | Gayatri Highways Ltd - 541546 - Completion Of Sale Of Stake In The Associate Company Pursuant To The Securitie | Completion of Sale of stake in the Associate Company
#38 score=6.20 dt=704s size=True (358902 vs 358905) sim=0.40
   N 2026-09-08 14:42:33.000000 | Change in Management | Arisinfra Solutions Limited has informed the Exchange regarding Resignation of Mr. Suvesh Prasad Sinha from Senior Management Personnel of the Company
   B 2026-09-08 14:30:48.517000 | Company Update / Change in Management | ArisInfra Solutions Ltd - 544419 - Announcement under Regulation 30 (LODR)-Change in Management | Intimation of change in Senior Management Personnel (SMP)
#39 score=5.50 dt=277s size=True (7277117 vs 7276841) sim=0.50
   N 2026-09-15 14:46:24.000000 | Outcome of Board Meeting | Sunshine Pictures Limited has submitted to the Exchange, the financial results for the period ended Jun 30, 2026.
   B 2026-09-15 14:41:46.783000 | Board Meeting / Outcome of Board Meeting | Sunshine Pictures Ltd - 544881 - Board Meeting Outcome for Outcome Of The Board Meeting Held On 15Th September | Un-audited Financial Results for the quarter ended 30th June, 2026
#40 score=7.00 dt=356s size=True (436460 vs 436455) sim=0.67
   N 2026-09-11 13:54:04.000000 | Outcome of Board Meeting | IIFL Capital Services Limited has informed the Exchange regarding Outcome of Board Meeting held on September 11, 2026.
   B 2026-09-11 13:48:07.937000 | Others / Outcome without intimation | IIFL Capital Services Ltd - 542773 - Board Meeting Outcome for Outcome Of The Meeting Of Board Of Directors | Outcome of the Board Meeting held on September 11, 2026
#41 score=5.00 dt=2859s size=True (847473 vs 847475) sim=0.33
   N 2026-09-05 11:06:00.000000 | Updates | Atal Realtech Limited has informed the Exchange regarding 'Clarification Letter'.
   B 2026-09-05 10:18:20.613000 | Company Update / General | Atal Realtech Ltd - 543911 - Clarification Letter | Please find enclosed the clarification letter on significant movement in Price of shares of the company
#42 score=7.00 dt=120s size=True (637102 vs 637098) sim=0.67
   N 2026-09-08 07:58:01.000000 | Credit Rating- New | Sportking India Limited has informed the Exchange about Credit Rating
   B 2026-09-08 08:00:00.890000 | Company Update / Credit Rating | Sportking India Ltd - 539221 - Announcement under Regulation 30 (LODR)-Credit Rating | Intimation of Credit Rating
#43 score=4.75 dt=973s size=True (671826 vs 671822) sim=0.25
   N 2026-09-10 12:43:23.000000 | Change in Auditors | NTPC Green Energy Limited has informed the Exchange regarding Change in Auditors of the company.
   B 2026-09-10 12:59:35.887000 | Company Update / Appointment of Statutory Auditor/s | NTPC Green Energy Ltd - 544289 - Announcement under Regulation 30 (LODR)-Appointment of Statutory Auditor/s | Please find enclosed herewith disclosure regarding appointment of statutory auditors of the Company for the FY
#44 score=6.09 dt=368s size=True (362301 vs 362305) sim=0.36
   N 2026-09-11 04:48:01.000000 | ESOP/ESOS/ESPS | ICICI Bank Limited has informed the Exchange regarding Allotment of 336265 Shares.
   B 2026-09-11 04:41:52.883000 | Company Update / Allotment of ESOP / ESPS | ICICI Bank Ltd - 532174 - Announcement under Regulation 30 (LODR)-Allotment of ESOP / ESPS | Allotment of 336,265 equity shares at FV Rs. 2 each under ICICI Bank Employees Stock Option Scheme 2000.
#45 score=4.80 dt=1041s size=True (654848 vs 654853) sim=0.27
   N 2026-09-09 14:12:43.000000 | Updates | Western Carriers (India) Limited has informed the Exchange regarding 'Letter to Members- for the Annual Report FY 2025-26'.
   B 2026-09-09 13:55:21.503000 | Company Update / General | Western Carriers (India) Ltd - 544258 - Letter To Members - The For Annual Report FY 2025-26 | The Company has issued the letters to those members whose email addresses are not registered with the Company/
#46 score=6.80 dt=519s size=True (321485 vs 321482) sim=0.60
   N 2026-09-15 15:02:51.000000 | Updates | eClerx Services Limited has informed the Exchange regarding 'Intimation of Schedule of Investor Meeting'.
   B 2026-09-15 15:11:30.447000 | Company Update / Analyst / Investor Meet | eClerx Services Ltd - 532927 - Announcement under Regulation 30 (LODR)-Analyst / Investor Meet - Intimation | Intimation of Schedule of Investor Meeting
#47 score=7.00 dt=7s size=True (1132462 vs 1132440) sim=1.00
   N 2026-09-04 09:49:31.000000 | Copy of Newspaper Publication | Ind-Swift Laboratories Limited has informed the Exchange about Copy of Newspaper Publication
   B 2026-09-04 09:49:23.630000 | Company Update / Newspaper Publication | Ind-Swift Laboratories Ltd - 532305 - Announcement under Regulation 30 (LODR)-Newspaper Publication | Please refer attached newspaper publication dated 04.09.2026.
#48 score=4.67 dt=1061s size=True (1740636 vs 1744102) sim=0.56
   N 2026-09-08 09:04:18.000000 | Shareholders meeting | Bharat Seats Limited has informed the Exchange regarding Notice of Postal Ballot
   B 2026-09-08 08:46:36.627000 | AGM/EGM / Postal Ballot | Bharat Seats Ltd-$ - 523229 - Shareholder Meeting / Postal Ballot-Notice of Postal Ballot | as per attachment.
#49 score=4.75 dt=1264s size=True (331295 vs 331294) sim=0.25
   N 2026-09-10 13:05:02.000000 | ESOP/ESOS/ESPS | Indusind Bank Limited has informed the Exchange regarding Exercise of 4160 Options.
   B 2026-09-10 12:43:57.950000 | Company Update / Allotment of ESOP / ESPS | Indusind Bank Ltd - 532187 - Announcement under Regulation 30 (LODR)-Allotment of ESOP / ESPS | Would like to inform Bank has allotted 4160 shares to those grantees who had exercised their options under Ban
#50 score=4.00 dt=549s size=True (4424991 vs 4421630) sim=0.00
   N 2026-09-07 10:31:50.000000 | Shareholders meeting | VLS Finance Limited has informed the Exchange about Shareholders meeting
   B 2026-09-07 10:22:40.520000 | Others / Reg. 34 (1) Annual Report | VLS Finance Ltd - 511333 - Reg. 34 (1) Annual Report. | Please find enclosed Annual Report of the Company for the Financial Year 2025-26.
```
