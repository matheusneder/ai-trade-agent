# OCI regions for the agent

> A decision matrix for the home region of the [move to OCI](oci-deployment.md). It covers every OCI commercial region on four dimensions. Regulation weighs most and latency least. Checked on 2026-11-26.

## 1. Recommendation

1. **Brazil East (São Paulo)**, `sa-saopaulo-1`. The IP matches the account's Brazilian KYC, and Binance holds Brazilian licences.
2. **Brazil Southeast (Vinhedo)**, `sa-vinhedo-1`. The case is the same as São Paulo's. No *Out of host capacity* report turned up for Vinhedo, unlike São Paulo, but that is weak evidence.
3. **UAE Central (Abu Dhabi)**, `me-abudhabi-1`. Use it only if the sign-up offers neither Brazilian region. Binance's global platform is regulated in Abu Dhabi, so the UAE is the country least likely to turn against Binance. The IP still does not match the KYC.

The US, Canada, the Netherlands and Malaysia are excluded. The rest of the EU, the UK, Japan, South Korea, Singapore and the remaining countries in the "Avoid" rows should be avoided. Latency changes none of this, because it does not matter to this agent ([oci-deployment.md](oci-deployment.md), §1).

## 2. Matrix

Verdicts and levels are defined in §4. *RTT* is the approximate round trip to Tokyo, where Binance's matching engine runs in AWS `ap-northeast-1`. *Reports* means anecdotal *Out of host capacity* posts.

| Country | Regions | Regulation | RTT to Tokyo | Cost | Always Free A1 | Verdict |
|---|---|---|---|---|---|---|
| Brazil | São Paulo `sa-saopaulo-1`, Vinhedo `sa-vinhedo-1` | **Good.** The KYC country; Binance is licensed locally | ~300 ms (measured) | Same, T2 | Yes; reports for São Paulo | **Recommended** |
| UAE | Abu Dhabi `me-abudhabi-1`, Dubai `me-dubai-1` | **Workable.** Binance's own regulator (ADGM); IP ≠ KYC | ~225 ms | Same, T3 | Yes; reports for Dubai | Fallback |
| Mexico | Querétaro `mx-queretaro-1`, Monterrey `mx-monterrey-1` | **Workable.** Binance operates openly (Medá, e-money licence); IP ≠ KYC | ~150 ms | Same, T1? | Yes | Fallback |
| Colombia | Bogotá `sa-bogota-1` | **Workable.** No crypto framework; Binance operates openly; IP ≠ KYC | ~245 ms | Same, T2 | Yes | Fallback |
| Chile | Santiago `sa-santiago-1`, Valparaíso `sa-valparaiso-1` | **Workable, unverified.** Binance operates; its CMF registration was not found; IP ≠ KYC | ~265 ms | Same, T2 | Yes | Fallback |
| Australia | Sydney `ap-sydney-1`, Melbourne `ap-melbourne-1` | **Workable.** Residents are served by Binance Australia; IP ≠ KYC | ~100–110 ms | Same, T2 | Yes | Fallback |
| South Africa | Johannesburg `af-johannesburg-1` | **Workable.** Binance acts through a licensed local FSP; IP ≠ KYC | ~360 ms | Same, T3 | Yes | Fallback |
| India | Mumbai `ap-mumbai-1`, Hyderabad `ap-hyderabad-1` | **Workable.** Registered with FIU-IND since 2024, after a URL block; IP ≠ KYC | ~115–120 ms | Same, T2 | Yes | Fallback |
| Switzerland | Zurich `eu-zurich-1` | **Workable, unverified.** Outside MiCA; Binance's Swiss status not checked; IP ≠ KYC | ~225 ms | Same, T1 | Yes | Fallback |
| Serbia | Jovanovac `eu-jovanovac-1` | **Unverified.** Binance's Serbian status not checked; IP ≠ KYC | ~245 ms | Same, T1 | **Unverified:** separate realm (OC20) | Avoid |
| EU: Germany, France, Italy, Spain, Sweden | Frankfurt `eu-frankfurt-1`, Paris `eu-paris-1`, Marseille `eu-marseille-1`, Milan `eu-milan-1`, Turin `eu-turin-1`, Madrid `eu-madrid-1` and `eu-madrid-3`, Stockholm `eu-stockholm-1` | **Avoid.** No MiCA licence; EU service cut back since 2026-07-01; regulators probing what remains | ~210–250 ms | Same, T1 | Yes; reports for Madrid | Avoid |
| UK | London `uk-london-1`, Newport `uk-cardiff-1` | **Avoid.** No new UK users since 2023-10-16; no FCA authorisation | ~220 ms | Same, T1 | Yes | Avoid |
| Japan | Tokyo `ap-tokyo-1`, Osaka `ap-osaka-1` | **Avoid.** binance.com stopped serving residents on 2023-11-30 (Binance Japan) | a few ms (Tokyo), ~8 ms (Osaka) | Same, T2 | Yes | Avoid |
| South Korea | Seoul `ap-seoul-1`, Chuncheon `ap-chuncheon-1` | **Avoid.** Unregistered; app blocked; FIU blocks unregistered exchanges' sites | ~30 ms | Same, T2 | Seoul yes; **Chuncheon no A1** | Avoid |
| Singapore | Singapore `ap-singapore-1`, Singapore West `ap-singapore-2` | **Avoid.** Binance left in 2021–22; on MAS's Investor Alert List | ~65 ms | Same, T2 | Yes | Avoid |
| Indonesia | Batam `ap-batam-1` | **Avoid.** Listed among illegal crypto traders by OJK's task force in 2026; residents go through Tokocrypto | ~65–90 ms | Same, T2 | Yes | Avoid |
| Israel | Jerusalem `il-jerusalem-1` | **Avoid.** Binance stopped Israel-focused activity in 2022 at the regulator's request | ~270 ms | Same, T3 | Yes; reports | Avoid |
| Saudi Arabia | Riyadh `me-riyadh-1`, Jeddah `me-jeddah-1` | **Avoid.** No exchange is licensed there, Binance included | ~300 ms | Same, T3 | Yes | Avoid |
| Morocco | Casablanca `af-casablanca-1` | **Avoid.** Crypto banned since 2017; the law to allow it is still a draft | ~250 ms | Same, T3 | Yes | Avoid |
| Malaysia | Kulai `ap-kulai-2` | **Excluded.** The government blocks Binance's website and app | ~70 ms | Same, T2 | Yes | Excluded |
| Netherlands | Amsterdam `eu-amsterdam-1` | **Excluded.** On Binance's List of Prohibited Countries | ~235 ms | Same, T1 | Yes | Excluded |
| Canada | Toronto `ca-toronto-1`, Montreal `ca-montreal-1` | **Excluded.** On the list (all of Canada, not only Ontario); Binance left in 2023 | ~145–185 ms | Same, T1 | Yes | Excluded |
| US | Ashburn `us-ashburn-1`, Chicago `us-chicago-1`, Phoenix `us-phoenix-1`, San Jose `us-sanjose-1` | **Excluded.** On the list; the API answers HTTP 451 | ~110–150 ms | Same, T1 | Yes | Excluded |

Out of scope: the government realms (US Government, US Defense, UK Sovereign, and Australia Government and Defence) and dedicated regions. A Free Tier account cannot use them.

## 3. Notes

- **Brazil:**
  - The Central Bank granted Binance Brasil Corretora a foreign-exchange licence, published on 2026-11-24.
  - The crypto licence (PSAV) under Resolutions 519–521 was filed on 2026-10-23 and is pending.
  - A Brazilian IP is the only one that matches the account's KYC. The production key will be restricted to it.
- **UAE:** Binance's Terms of Use (effective 2026-10-27) are a contract with its Abu Dhabi entities, regulated by the ADGM's FSRA. That puts the UAE ahead of Mexico, not latency.
- **EU:**
  - Frankfurt used to be doc 01's example of an allowed region. It no longer is.
  - In June 2026, Binance withdrew its MiCA application in Greece. Since 2026-07-01 it has taken no new EU users and offered EU residents only withdrawals and position management.
  - Since October 2026, ESMA and the regulators of France, Germany and Greece have been examining how Binance still serves EU users under the reverse-solicitation exemption.
  - No source says the API refuses EU IPs. But a non-EU account trading around the clock from an EU data centre is exactly what that review looks for.
- **Japan:** Japan is not on the List of Prohibited Countries, so the API may well answer from Japanese IPs. That is unverified. The account would still be trading from a country where binance.com no longer serves residents.
- **South Korea:**
  - Google Play has blocked Binance's app since 2026-01-28.
  - The FIU's 2026-10-13 order blocked the websites of 27 unregistered exchanges. The list published by The Paypers does not name Binance, but Crypto Briefing says Binance was affected. Either way, a later round could cut the API off on Korean networks.
  - South Korea North (Chuncheon) is the one region where Oracle's docs exclude Always Free A1.
- **Malaysia:** in July 2026, the Finance Ministry said Binance's website and app remain blocked. The Securities Commission put Binance back on its Investor Alert List in October 2026.
- **India:** India blocked Binance's URLs from late 2023 until its FIU registration in August 2024. A lapse could bring the block back.
- **Serbia:** Jovanovac is the only commercial region in its own realm (OC20), with its own console. A tenancy in the main realm (OC1) cannot reach it, and whether the Free Tier sign-up offers it is unverified.
- **Australia:** whether an Australian IP would push the account towards the local Binance Australia entity is unverified.

## 4. Method

- **Regulation:** scored from Binance's own documents first, then from regulators and the press (§5). Three questions per country:
  - **Access:** is the country on Binance's List of Prohibited Countries, or has Binance left it or stopped serving its residents? The list was updated on 2026-01-05 and names the US, Canada, the Netherlands and sanctioned territories. The Terms of Use, §25.5, say access from a listed location may be blocked, and forbid getting around that with a VPN or similar.
  - **Network:** does the country block unregistered exchanges at the network level?
  - **KYC:** is the IP a compliance signal for a Brazilian-KYC account? Every country but Brazil is a mismatch. So this question separates *Recommended* from the rest, and the other two questions rank the rest.

  The levels and the verdicts follow from those questions:
  - **Good** gives *Recommended*: Binance is licensed locally, nothing is blocked, and the IP matches the KYC.
  - **Workable** gives *Fallback*: Binance serves the country openly and nothing blocks it, but the IP does not match the KYC.
  - **Avoid** gives *Avoid*: Binance left the country, does not serve its residents, or is under regulatory pressure there, or the country blocks unregistered exchanges. The API may answer today and stop without notice.
  - **Excluded** gives *Excluded*: the country is on the prohibited list, or its government already blocks Binance.
  - **Unverified** marks a row where a point the verdict depends on could not be checked. Serbia is *Avoid* because its free-tier availability is also unverified.
- **Latency:** the RTT is WonderNetwork's average ping between hosts in each city and Tokyo, read on 2026-11-26. It is public internet between hosting providers, not OCI to AWS. Brazil's figure is the one measured from Brazil.
  - Where a region's city is missing, the nearest listed city stands in:
    - Batam: Singapore and Jakarta;
    - Kulai: Kuala Lumpur;
    - Querétaro and Monterrey: Mexico City;
    - Casablanca: Fez;
    - Jeddah: Riyadh;
    - Abu Dhabi: Dubai;
    - Turin: Milan;
    - Valparaíso: Santiago;
    - Chuncheon: Seoul;
    - Ashburn: Washington;
    - Newport: Cardiff.
  - Tokyo has no figure because OCI Tokyo and AWS Tokyo share the metro area.
  - Latency is scored last and changes no verdict.
- **Cost:** OCI charges the same price for a service in every region, and the Always Free allowance is the same in each home region. That allowance is 1,500 OCPU-hours and 9,000 GB-hours of A1 a month, 200 GB of block storage and 10 TB of outbound data.
  - The only regional difference is outbound data above the free 10 TB a month:
    - **T1:** North America, Europe and the UK, US$ 0.0085/GB;
    - **T2:** APAC, Japan and South America, US$ 0.025/GB;
    - **T3:** the Middle East and Africa, US$ 0.05/GB.
  - The price list does not place Mexico, so T1 is assumed (T1?).
  - The agent's traffic is far below 10 TB, so no tier applies.
- **Always Free A1:** Oracle's Free Tier FAQ says the Free Tier is available wherever commercial OCI is, but that the exact regions "may differ during signup". The documented exceptions are Chuncheon (no A1) and Serbia (a separate realm).
  - The capacity reports are anecdotal forum posts. A region without one only means no post was found, not that it has capacity.
  - The Pay As You Go upgrade raises the priority for A1 capacity ([oci-deployment.md](oci-deployment.md), §3).

## 5. Sources (checked on 2026-11-26)

- **Oracle:**
  - [Regions and availability domains](https://docs.oracle.com/en-us/iaas/Content/General/Concepts/regions.htm)
  - [Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
  - [Free Tier](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm)
  - [Free Tier FAQ](https://www.oracle.com/cloud/free/faq/)
  - [Pricing](https://www.oracle.com/cloud/pricing/)
  - [Price list, networking](https://www.oracle.com/cloud/price-list/#pricing-networking)
  - [Serbia realm](https://docs.oracle.com/en/solutions/oci-serbia-realm/index.html)
- **Capacity reports:**
  - [São Paulo](https://community.oracle.com/customerconnect/discussion/949983/cannot-upgrade-free-tier-to-payg-a1-flex-out-of-capacity-in-sa-saopaulo-1-for-3-days)
  - [Dubai](https://community.oracle.com/customerconnect/discussion/707019/out-of-capacity-me-dubai-1)
  - [Madrid](https://forums.oracle.com/ords/apexds/post/how-long-to-wait-in-new-region-madrid-before-free-tier-host-7924)
  - [Jerusalem](https://forums.oracle.com/ords/apexds/post/request-to-enable-additional-region-for-always-free-tier-i-4815)
- **Binance:**
  - [List of Prohibited Countries](https://www.binance.com/en/legal/list-of-prohibited-countries)
  - [Terms of Use](https://www.binance.com/en/terms)
  - [Discontinuation for residents of Japan](https://www.binance.com/en-JP/support/announcement/discontinuation-of-binance-global-platform-for-residents-in-japan-f347c6a7797d4d4190d20edc4f350098)
- **Brazil:**
  - [Exame: foreign-exchange licence](https://exame.com/future-of-money/binance-recebe-autorizacao-do-bc-para-operar-no-mercado-de-cambio/)
  - [Cointelegraph: PSAV application](https://cointelegraph.com/news/binance-brazil-crypto-license-oct-30-deadline)
- **EU:**
  - [CoinDesk, 2026-06-26](https://www.coindesk.com/policy/2026/06/26/binance-tells-eu-users-it-will-no-longer-provide-services-after-failing-to-secure-mica-license)
  - [Decrypt, 2026-10-01](https://decrypt.co/379792/eu-presses-binance-over-reverse-solicitation-exemption-for-users-report)
- **UK:**
  - [Cointelegraph, 2023](https://cointelegraph.com/news/binance-halts-new-uk-user-onboarding)
  - [The Cryptonomist, 2026-08](https://en.cryptonomist.ch/2026/08/17/binance-fca-license-uk/)
- **South Korea:**
  - [The Block: Google Play](https://www.theblock.co/post/385892/google-play-ban-south-korea)
  - [The Paypers: 27 sites](https://thepaypers.com/crypto-web3-and-cbdc/news/south-korea-orders-block-on-27-unregistered-crypto-exchange-sites)
  - [Crypto Briefing](https://cryptobriefing.com/south-korea-blocks-27-overseas-crypto-exchanges/)
- **Malaysia:** [Malay Mail, 2026-07-17](https://www.malaymail.com/news/malaysia/2026/07/17/finance-ministry-says-binance-not-licensed-in-malaysia-website-and-app-access-still-blocked/227919)
- **Singapore:**
  - [MAS on DTSPs](https://www.mas.gov.sg/news/media-releases/2025/mas-clarifies-regulatory-regime-for-digital-token-service-providers)
  - [CoinGecko](https://www.coingecko.com/learn/best-crypto-exchanges-in-singapore)
- **India:** [TechCrunch, 2024-08-15](https://techcrunch.com/2024/08/15/binance-restarts-services-in-india-after-seven-month-regulatory-halt)
- **Indonesia:** [Infobank, Satgas PASTI list](https://infobanknews.com/satgas-pasti-tutup-228-pedagang-kripto-ilegal-ini-daftar-lengkapnya/)
- **Israel:**
  - [Cointelegraph, 2022](https://cointelegraph.com/news/binance-stopped-all-activities-focused-on-israel-following-regulatory-request-report)
  - [Shibolet, 2026-11](https://www.shibolet.com/?p=28163)
- **Saudi Arabia:** [Argaam](https://www.argaam.com/en/article/articledetail/id/1782778)
- **Morocco:** [Morocco World News, 2026-07](https://www.moroccoworldnews.com/2026/07/330814/bam-confirms-progress-on-crypto-law-to-curb-moroccos-growing-underground-market/)
- **Mexico:** [Cointelegraph: Medá](https://cointelegraph.com/news/binance-mexico-expansion-meda-53m-investment)
- **Colombia:** [Latin Times](https://www.latintimes.com/latin-america-crypto-regulation-brazil-vasp-deadline-600659)
- **South Africa:** [BusinessTech, 2026-05](https://businesstech.co.za/news/industry-news/860594/binance-introduces-tradfi-perpetual-contracts-and-enhances-futures-trading-experience-in-south-africa/)
- **Australia:** [ASIC 26-055MR](https://asic.gov.au/about-asic/news-centre/find-a-media-release/2026-releases/26-055mr-binance-australia-derivatives-ordered-to-pay-10-million-penalty-for-onboarding-failures-causing-millions-in-client-trading-losses/)
- **Canada:** [BNN Bloomberg, 2023-05-12](https://www.bnnbloomberg.ca/binance-to-stop-serving-canadian-market-blames-new-stablecoin-guidance-1.1919585)
- **Latency:** [WonderNetwork, pings to Tokyo](https://wondernetwork.com/pings/Tokyo)
- **Not verified:**
  - whether the API refuses Japanese, EU, Canadian or Dutch IPs (only US IPs are known to get HTTP 451);
  - Binance's status in Chile, Switzerland and Serbia;
  - whether the October 2026 Korean block named Binance;
  - whether the Free Tier sign-up offers Serbia or each Brazilian region.
