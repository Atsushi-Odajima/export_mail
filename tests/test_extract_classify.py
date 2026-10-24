"""抽出・判定・分類の試験（架空データ）。"""
from bs4 import BeautifulSoup

from sdr import classify as C
from sdr.extract import classify_form, decode_cfemail, parse_page, valid_email
from conftest import fixture_text

BASE = "https://www.test-motors.example"


def test_emails_from_mailto_text_and_protected_display():
    info = parse_page(fixture_text("contact.html"), BASE + "/contact-us")
    found = {e.email: e for e in info.emails}
    assert found["sales@test-motors.example"].method == "mailto"
    assert found["info@test-motors.example"].method == "保護表示の復号"
    assert "jobs@test-motors.example" in found
    assert "privacy@test-motors.example" in found


def test_image_names_and_placeholders_are_not_emails():
    assert not valid_email("logo@2x.png")
    assert not valid_email("name@example.com")
    assert not valid_email("canvas-confetti@1.9.3")
    info = parse_page(fixture_text("home.html"), BASE + "/")
    assert "logo@2x.png" not in {e.email for e in info.emails}


def test_decode_cfemail():
    assert decode_cfemail("2a43444c456a5e4f595e0747455e455859044f524b475a464f") == "info@test-motors.example"


def test_shop_email_vs_agency_recruitment_privacy():
    host = "www.test-motors.example"
    assert C.judge_email("sales@test-motors.example", host).status == C.EMAIL_STATUS_OK
    agency = C.judge_email("hello@pixelagency.example", host, "Website by Pixel Agency hello@pixelagency.example")
    assert agency.status == C.EMAIL_STATUS_EXCLUDED and "制作会社" in agency.reason
    assert C.judge_email("jobs@test-motors.example", host).status == C.EMAIL_STATUS_EXCLUDED
    assert C.judge_email("privacy@test-motors.example", host).status == C.EMAIL_STATUS_EXCLUDED
    assert C.judge_email("dpo@test-motors.example", host).reason == "個人情報・プライバシー窓口"
    other = C.judge_email("office@other-domain.example", host)
    assert other.status == C.EMAIL_STATUS_REVIEW  # 別ドメインは自動採用しない
    assert C.judge_email("testmotors@gmail.com", host).status == C.EMAIL_STATUS_OK
    assert C.judge_email("noreply@test-motors.example", host).status == C.EMAIL_STATUS_EXCLUDED


def _form(html):
    return classify_form(BeautifulSoup(html, "html.parser").form)


def test_contact_form_distinguished_from_search_login_newsletter():
    assert _form('<form><input name="name"><input type="email" name="email"><textarea name="message"></textarea></form>').kind == "問い合わせ"
    assert _form('<form role="search"><input name="q"></form>').kind == "検索"
    assert _form('<form action="/search"><input type="text" name="s"><button>Go</button></form>').kind == "検索"
    assert _form('<form><input name="user"><input type="password" name="pw"></form>').kind == "ログイン"
    assert _form('<form><p>Subscribe to our newsletter</p><input type="email" name="email"></form>').kind == "ニュースレター"
    assert _form('<form><select name="make"></select><select name="model"></select><select name="maxprice"></select></form>').kind == "在庫検索"


def test_embedded_form_provider_detected():
    info = parse_page('<html><body><h1>Contact</h1><script src="https://js.hsforms.net/forms/v2.js"></script></body></html>', BASE + "/contact")
    assert "HubSpot フォーム" in info.embeds
    assert not [f for f in info.forms if f.kind == "問い合わせ"]


def page(role, text, headline=""):
    return C.PageText(BASE + "/" + role, role, headline, headline, text)


def test_official_specialist_claim():
    cls = C.classify_dealer([page("home", "Welcome. We are classic car specialists based in Kent with classic cars for sale.",
                                  "Example Classics | Classic Car Specialists")])
    assert cls.categories == [C.CAT_CLASSIC]
    assert cls.basis == C.BASIS_OFFICIAL
    assert cls.evidence and "classic" in cls.evidence[0][2].lower()


def test_few_suvs_do_not_make_suv_specialist():
    stock = " ".join(["2018 Ford Fiesta £8,995", "2017 Vauxhall Corsa £6,495", "2019 Nissan Qashqai £13,995",
                      "2016 VW Golf £9,250", "2015 Toyota Yaris £7,995", "2020 Kia Picanto £8,250"])
    cls = C.classify_dealer([page("home", "Used car dealer in Leeds", "Used car dealer"), page("stock", stock)])
    assert C.CAT_SUV not in cls.categories
    assert cls.categories == [C.CAT_GENERAL]
    assert cls.basis == C.BASIS_TREND
    assert "全在庫は未確認" in cls.scope


def test_stock_trend_without_specialist_claim():
    models = ["Range Rover Sport £24,995", "Land Rover Discovery 4 £19,995", "Volvo XC90 £21,500", "Kia Sorento £15,995",
              "Toyota RAV4 £17,250", "Range Rover Evoque £16,995", "Land Rover Defender 110 £39,995"]
    cls = C.classify_dealer([page("home", "Welcome to our showroom"), page("stock", " ".join(models))])
    assert cls.categories == [C.CAT_SUV]
    assert cls.basis == C.BASIS_TREND


def test_unknown_stays_review():
    cls = C.classify_dealer([page("home", "Welcome to our website. Opening hours Monday to Friday.")])
    assert cls.categories == [C.CAT_REVIEW] and cls.basis == C.BASIS_MANUAL
    summary = C.build_summary(None, None, "未確認", cls, [])
    assert "要確認" in summary and "未確認" in summary


def test_summary_only_states_found_facts():
    cls = C.Classification([C.CAT_GENERAL], C.BASIS_TREND, "在庫ページ 1 件", [])
    s = C.build_summary("ヨークシャー", "LS1 4AP", "中古車販売を確認", cls, ["下取り（Part Exchange）", "1998年創業の記載"])
    assert s.startswith("ヨークシャー（郵便番号 LS1 4AP）の中古車販売店です。")
    assert "全在庫は未確認" in s
    assert s.count("。") <= 3


def test_ireland_not_treated_as_uk():
    st, _ = C.detect_uk([], ["+353 1 000 0000"], "Located in Co. Dublin, Republic of Ireland", "cars.example.ie")
    assert "英国外" in st
    st, _ = C.detect_uk([("BT1 1AA", "", "")], [], "Belfast, Northern Ireland", "ni-cars.example")
    assert st == "英国内の住所を確認"


def test_marque_specialist_is_sports_official():
    cls = C.classify_dealer([page("home", "Passion for Perfection. Independent Low Mileage Porsche Specialists. Cars for sale.")])
    assert cls.categories == [C.CAT_SPORTS] and cls.basis == C.BASIS_OFFICIAL


def test_car_and_van_dealer_is_not_van_specialist():
    cls = C.classify_dealer([page("home", "Used cars for sale in Edinburgh",
                                  "Used cars and vans Edinburgh, Used car and van Dealer in Midlothian")])
    assert C.CAT_OTHER not in cls.categories and cls.categories == [C.CAT_GENERAL]


def test_match_does_not_cross_sentences():
    text = ("We are experts in the storage and transport of classic cars, motorcycles and modern high performance machines. "
            "People At The Hub Sales team")
    cls = C.classify_dealer([page("about", text)])
    assert C.CAT_SPORTS not in cls.categories


def test_foreign_structured_address_flagged():
    from sdr.research import _foreign_jsonld_address
    assert _foreign_jsonld_address([{"@type": "AutoDealer", "address": {"postalCode": "151-0063", "addressLocality": "Tokyo"}}])
    assert _foreign_jsonld_address([{"address": {"postalCode": "LS1 4AP", "addressCountry": "GB"}}]) is None
    assert _foreign_jsonld_address([{"address": {"postalCode": "D02 X285", "addressCountry": "IE"}}])


def test_outward_postcode_and_times_sign():
    from sdr.research import _foreign_jsonld_address
    assert _foreign_jsonld_address([{"address": {"postalCode": "CM9", "addressCountry": "GB"}}]) is None
    cls = C.classify_dealer([page("about", "Family run, independent, new and used 4×4 vehicle specialists in Norfolk.")])
    assert C.CAT_SUV in cls.categories


def test_address_cleanup():
    from sdr.research import address_from_context
    ctx = "mail.com Contact us on: 07933 447710 Find us at: Woodhall Cars Calverley Site, A6120 Ring Rd, Calverley, Leeds, LS13 1LY"
    assert address_from_context(ctx) == "Woodhall Cars Calverley Site, A6120 Ring Rd, Calverley, Leeds, LS13 1LY"
    ctx2 = "Contact 01274 738500 WhatsApp Hillam Road Off Canal Road Bradford West Yorkshire BD2 1QN"
    assert address_from_context(ctx2) == "Hillam Road Off Canal Road Bradford West Yorkshire BD2 1QN"
