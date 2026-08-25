"""Unit tests for retrieval/value_retriever.py (ValueRetriever)."""

from __future__ import annotations

from retrieval.value_retriever import ValueRetriever


class TestExtractYear:

    def test_extracts_ascii_year(self):
        assert ValueRetriever.extract_year("معاملات سال 1402") == 1402

    def test_extracts_persian_digit_year(self):
        assert ValueRetriever.extract_year("معاملات سال ۱۴۰۲") == 1402

    def test_persian_digits_without_space(self):
        assert ValueRetriever.extract_year("معاملات سال۱۴۰۲") == 1402

    def test_extracts_arabic_indic_digit_year(self):
        assert ValueRetriever.extract_year("معاملات سال ١٤٠٢") == 1402

    def test_returns_none_without_year(self):
        assert ValueRetriever.extract_year("معاملات فروردین") is None


class TestExtractMonthName:

    def test_extracts_month_name(self):
        assert ValueRetriever.extract_month_name("معاملات اردیبهشت") == "اردیبهشت"

    def test_extracts_esfand(self):
        assert ValueRetriever.extract_month_name("قراردادهای اسفند ۱۴۰۲") == "اسفند"

    def test_returns_none_without_month(self):
        assert ValueRetriever.extract_month_name("معاملات فصل بهار") is None

    def test_returns_none_without_persian_month(self):
        assert ValueRetriever.extract_month_name("خرید مشتریان") is None


class TestExtractDayOfWeek:

    def test_shanbe_is_one(self):
        assert ValueRetriever.extract_day_of_week("معاملات شنبه") == 1

    def test_yekshanbe_is_two(self):
        assert ValueRetriever.extract_day_of_week("خرید یکشنبه") == 2

    def test_panjshanbe_is_six(self):
        assert ValueRetriever.extract_day_of_week("فروش پنجشنبه") == 6

    def test_jomeh_is_seven(self):
        assert ValueRetriever.extract_day_of_week("معاملات جمعه") == 7

    def test_spaced_variant_matches(self):
        assert ValueRetriever.extract_day_of_week("معاملات پنج شنبه") == 6

    def test_longest_name_beats_shanbe_substring(self):
        assert ValueRetriever.extract_day_of_week("معاملات یکشنبه") == 2

    def test_returns_none_without_day(self):
        assert ValueRetriever.extract_day_of_week("معاملات فروردین") is None


class TestExtractSeasonName:

    def test_extracts_bahar(self):
        assert ValueRetriever.extract_season_name("معاملات فصل بهار") == "بهار"

    def test_extracts_tabestan(self):
        assert ValueRetriever.extract_season_name("حجم عرضه تابستان") == "تابستان"

    def test_extracts_payiz(self):
        assert ValueRetriever.extract_season_name("خرید مشتریان پاییز") == "پاییز"

    def test_extracts_zemestan(self):
        assert ValueRetriever.extract_season_name("معاملات زمستان") == "زمستان"

    def test_returns_none_without_season(self):
        assert ValueRetriever.extract_season_name("خرید مشتریان") is None

    def test_month_name_is_not_season(self):
        assert ValueRetriever.extract_season_name("معاملات تیر") is None


class TestExtractPersianDate:

    def test_extracts_ascii_date(self):
        assert ValueRetriever.extract_persian_date("معاملات 1402/05/15") == "1402/05/15"

    def test_extracts_persian_digit_date(self):
        assert ValueRetriever.extract_persian_date("معاملات ۱۴۰۲/۰۵/۱۵") == "1402/05/15"

    def test_extracts_arabic_indic_digit_date(self):
        assert ValueRetriever.extract_persian_date("معاملات ١٤٠٢/٠٥/١٥") == "1402/05/15"

    def test_accepts_dash_separator(self):
        assert ValueRetriever.extract_persian_date("معاملات 1402-05-15") == "1402/05/15"

    def test_zero_pads_unpadded_components(self):
        assert ValueRetriever.extract_persian_date("معاملات 1402/5/7") == "1402/05/07"

    def test_rejects_gregorian_date(self):
        assert ValueRetriever.extract_persian_date("معاملات 2023/05/15") is None

    def test_rejects_invalid_month(self):
        assert ValueRetriever.extract_persian_date("معاملات 1402/13/15") is None

    def test_rejects_invalid_day(self):
        assert ValueRetriever.extract_persian_date("معاملات 1402/05/32") is None

    def test_rejects_month_31_day(self):
        assert ValueRetriever.extract_persian_date("معاملات 1402/11/31") is None

    def test_accepts_esfand_30(self):
        assert ValueRetriever.extract_persian_date("معاملات 1402/12/30") == "1402/12/30"

    def test_returns_none_without_date(self):
        assert ValueRetriever.extract_persian_date("معاملات فروردین") is None

    def test_returns_none_for_bare_year(self):
        assert ValueRetriever.extract_persian_date("معاملات سال 1402") is None

    def test_returns_none_for_phone_number(self):
        assert ValueRetriever.extract_persian_date("تماس 09121402158") is None


class TestRetrieve:

    def test_extracts_ring_year_month_day(self):
        filters = ValueRetriever.retrieve(
            "خرید مشتریان در تالار پتروشیمی اردیبهشت 1402 پنجشنبه"
        )
        assert filters == {
            "Ring": "تالار پتروشیمی و فرآورده های نفتی",
            "PersianYear": 1402,
            "PersianMonthName": "اردیبهشت",
            "PersianDayOfWeek": 6,
        }

    def test_no_date_terms_returns_ring_only(self):
        filters = ValueRetriever.retrieve("بیشترین خرید در تالار سیمان")
        assert filters == {"Ring": "تالار سیمان"}

    def test_extracts_ring_year_month_day_with_persian_digits(self):
        filters = ValueRetriever.retrieve(
            "خرید مشتریان در تالار پتروشیمی اردیبهشت ۱۴۰۲ پنجشنبه"
        )
        assert filters == {
            "Ring": "تالار پتروشیمی و فرآورده های نفتی",
            "PersianYear": 1402,
            "PersianMonthName": "اردیبهشت",
            "PersianDayOfWeek": 6,
        }

    def test_extracts_season_with_ring_and_year(self):
        filters = ValueRetriever.retrieve(
            "بیشترین حجم معامله در تالار پتروشیمی در فصل بهار ۱۴۰۲"
        )
        assert filters == {
            "Ring": "تالار پتروشیمی و فرآورده های نفتی",
            "PersianYear": 1402,
            "PersianSeasonName": "بهار",
        }

    def test_full_date_suppresses_year(self):
        filters = ValueRetriever.retrieve("معاملات 1402/05/15")
        assert filters == {"PersianDate": "1402/05/15"}

    def test_full_date_with_ring(self):
        filters = ValueRetriever.retrieve("معاملات در تالار پتروشیمی 1402/05/15")
        assert filters == {"Ring": "تالار پتروشیمی و فرآورده های نفتی", "PersianDate": "1402/05/15"}

    def test_persian_digit_full_date(self):
        filters = ValueRetriever.retrieve("خرید مشتریان در ۱۴۰۲/۰۵/۱۵")
        assert filters == {"PersianDate": "1402/05/15"}

    def test_invalid_full_date_falls_back_to_year(self):
        filters = ValueRetriever.retrieve("معاملات 1402/13/15")
        assert filters == {"PersianYear": 1402}
