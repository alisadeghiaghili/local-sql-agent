# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Generic vocabulary for the synthetic retrieval benchmark.

Plain English nouns with a Persian rendering, nothing taken from any real
deployment: the benchmark composes table names, labels and questions out of
these, so a large schema with realistic lexical overlap (``Product`` /
``ProductCategory`` / ``ProductAddress``) can be generated from a seed.

Every entry is ``(english, persian)``. English is a single word and becomes
CamelCase inside table names; Persian is the label a user would type, written
with ZWNJ (``U+200C``) where Persian orthography uses one, so that the
normalisation in :func:`core.persian.normalize_for_matching` is exercised.
"""

from __future__ import annotations

ZWNJ = "\u200c"

#: Things a table can be about (dimension tables and the "head" of a name).
NOUNS: tuple[tuple[str, str], ...] = (
    ("Customer", "مشتری"),
    ("Product", "محصول"),
    ("Store", "فروشگاه"),
    ("Employee", "کارمند"),
    ("Supplier", f"تأمین{ZWNJ}کننده"),
    ("Region", "منطقه"),
    ("City", "شهر"),
    ("Warehouse", "انبار"),
    ("Vehicle", "خودرو"),
    ("Driver", "راننده"),
    ("Route", "مسیر"),
    ("Contract", "قرارداد"),
    ("Account", "حساب"),
    ("Branch", "شعبه"),
    ("Department", "بخش"),
    ("Project", "پروژه"),
    ("Asset", "دارایی"),
    ("Machine", "ماشین"),
    ("Material", "ماده"),
    ("Brand", "برند"),
    ("Channel", "کانال"),
    ("Campaign", "کمپین"),
    ("Promotion", "پروموشن"),
    ("Bank", "بانک"),
    ("Policy", f"بیمه{ZWNJ}نامه"),
    ("Patient", "بیمار"),
    ("Doctor", "پزشک"),
    ("Course", "درس"),
    ("Student", "دانشجو"),
    ("Teacher", "معلم"),
    ("Carrier", f"حمل{ZWNJ}کننده"),
    ("Event", "رویداد"),
    ("Device", "دستگاه"),
    ("Sensor", "حسگر"),
    ("Meter", "کنتور"),
    ("Plant", "کارخانه"),
    ("Tender", "مناقصه"),
    ("Auction", "حراج"),
    ("Vendor", "فروشنده"),
    ("Buyer", "خریدار"),
    ("Agent", "نماینده"),
    ("Manager", "مدیر"),
    ("Team", "تیم"),
    ("Site", "سایت"),
    ("Building", "ساختمان"),
    ("Room", "اتاق"),
    ("Tariff", "تعرفه"),
    ("License", "مجوز"),
    ("Certificate", "گواهی"),
    ("Inspector", "بازرس"),
    ("Auditor", "حسابرس"),
    ("Tenant", "مستأجر"),
    ("Parcel", "قطعه"),
    ("Fleet", "ناوگان"),
    ("Terminal", "پایانه"),
    ("Port", "بندر"),
    ("Airport", "فرودگاه"),
    ("Station", "ایستگاه"),
    ("Crew", "خدمه"),
    ("Pilot", "خلبان"),
    ("Passenger", "مسافر"),
    ("Guest", "مهمان"),
    ("Hotel", "هتل"),
    ("Restaurant", "رستوران"),
    ("Recipe", "دستور پخت"),
    ("Farm", "مزرعه"),
    ("Crop", "غله"),
    ("Animal", "دام"),
    ("Field", "زمین"),
    ("Well", "چاه"),
    ("Mine", "معدن"),
    ("Pipeline", "خط لوله"),
    ("Reservoir", "مخزن"),
    ("Turbine", "توربین"),
    ("Battery", "باتری"),
    ("Panel", "پنل"),
    ("School", "مدرسه"),
    ("Classroom", "کلاس"),
    ("Library", "کتابخانه"),
    ("Book", "کتاب"),
    ("Author", "نویسنده"),
    ("Publisher", "ناشر"),
    ("Museum", "موزه"),
    ("Artist", "هنرمند"),
    ("Player", "بازیکن"),
    ("Coach", "مربی"),
    ("Lender", f"وام{ZWNJ}دهنده"),
    ("Insurer", f"بیمه{ZWNJ}گر"),
    ("Landlord", "مالک"),
    ("Technician", "تکنسین"),
)

#: Suffixes that name a classification of a noun (the parent table in a
#: snowflake: ``Product`` -> ``ProductCategory``).
CLASSIFIERS: tuple[tuple[str, str], ...] = (
    ("Category", "دسته"),
    ("Group", "گروه"),
    ("Type", "نوع"),
    ("Class", "رده"),
    ("Tier", "سطح"),
    ("Family", "خانواده"),
)

#: Suffixes that name an aspect of a noun (``CustomerAddress``).
ASPECTS: tuple[tuple[str, str], ...] = (
    ("Address", "نشانی"),
    ("Contact", "تماس"),
    ("Profile", "نمایه"),
    ("Rating", "امتیاز"),
    ("Note", "یادداشت"),
    ("Document", "سند"),
    ("Status", "وضعیت"),
    ("Schedule", "برنامه"),
)

#: Things that happen (fact tables).
EVENTS: tuple[tuple[str, str], ...] = (
    ("Order", "سفارش"),
    ("Shipment", "ارسال"),
    ("Invoice", "فاکتور"),
    ("Payment", "پرداخت"),
    ("Claim", "خسارت"),
    ("Visit", "بازدید"),
    ("Enrollment", f"ثبت{ZWNJ}نام"),
    ("Booking", "رزرو"),
    ("Reading", "قرائت"),
    ("Movement", "جابجایی"),
    ("Adjustment", "تعدیل"),
    ("Return", "مرجوعی"),
    ("Purchase", "خرید"),
    ("Delivery", "تحویل"),
    ("Inspection", "بازرسی"),
    ("Maintenance", "نگهداری"),
    ("Transfer", "انتقال"),
    ("Refund", "بازپرداخت"),
    ("Complaint", "شکایت"),
    ("Request", "درخواست"),
    ("Appointment", "نوبت"),
    ("Transaction", "تراکنش"),
    ("Withdrawal", "برداشت"),
    ("Deposit", "واریز"),
    ("Loan", "وام"),
    ("Trip", "سفر"),
    ("Rental", "اجاره"),
    ("Repair", "تعمیر"),
    ("Production", "تولید"),
    ("Consumption", "مصرف"),
    ("Sale", "فروش"),
    ("Bid", "پیشنهاد"),
    ("Registration", "ثبت"),
    ("Subscription", "اشتراک"),
    ("Incident", "حادثه"),
    ("Exam", "امتحان"),
    ("Attendance", "حضور"),
    ("Download", "دانلود"),
)

#: Suffixes that turn an event into a different fact table (``OrderLine``).
FACT_QUALIFIERS: tuple[tuple[str, str], ...] = (
    ("", ""),
    ("Line", "قلم"),
    ("Daily", "روزانه"),
    ("Summary", "خلاصه"),
    ("Detail", "جزئیات"),
)

#: Numeric columns of fact tables; a question may name one instead of the table.
MEASURES: tuple[tuple[str, str], ...] = (
    ("Revenue", "درآمد"),
    ("Quantity", "مقدار"),
    ("Amount", "مبلغ"),
    ("Cost", "هزینه"),
    ("Discount", "تخفیف"),
    ("Tax", "مالیات"),
    ("Weight", "وزن"),
    ("Volume", "حجم"),
    ("Duration", "مدت"),
    ("Distance", "مسافت"),
    ("Fuel", "سوخت"),
    ("Temperature", "دما"),
    ("Score", "نمره"),
    ("Margin", "حاشیه سود"),
    ("Commission", "کارمزد"),
    ("Penalty", "جریمه"),
    ("Levy", "عوارض"),
    ("Interest", "بهره"),
    ("Balance", "مانده"),
    ("Rate", "نرخ"),
    ("Price", "قیمت"),
    ("Tonnage", "تناژ"),
    ("Area", "مساحت"),
    ("Speed", "سرعت"),
    ("Pressure", "فشار"),
    ("Voltage", "ولتاژ"),
    ("Energy", "انرژی"),
    ("Humidity", "رطوبت"),
    ("Yield", "بازده"),
    ("Waste", "ضایعات"),
    ("Downtime", "توقف"),
    ("Delay", "تأخیر"),
    ("Premium", f"حق{ZWNJ}بیمه"),
    ("Deductible", "فرانشیز"),
    ("Salary", "حقوق"),
    ("Bonus", "پاداش"),
    ("Overtime", f"اضافه{ZWNJ}کار"),
    ("Stock", "موجودی"),
    ("Capacity", "ظرفیت"),
    ("Load", "بار"),
    ("Emission", "آلایندگی"),
    ("Royalty", f"حق{ZWNJ}امتیاز"),
)

#: Modifiers that turn a measure into a more specific column (``NetWeight``);
#: a fact's "signature" measure is one such combination, unique to it.
MEASURE_MODIFIERS: tuple[tuple[str, str], ...] = (
    ("Net", "خالص"),
    ("Gross", "ناخالص"),
    ("Base", "پایه"),
    ("Final", "نهایی"),
    ("Planned", f"برنامه{ZWNJ}ریزی{ZWNJ}شده"),
    ("Actual", "واقعی"),
)

#: Shared dimensions (a table that exists in more than one data source).
SHARED: tuple[tuple[str, str], ...] = (
    ("Calendar", "تقویم"),
    ("Currency", "ارز"),
    ("Country", "کشور"),
)

#: Words that mean "by period" -- the calendar dimension is selected by these.
TIME_WORDS_EN: tuple[str, ...] = ("month", "year", "quarter", "week", "day", "date")
TIME_WORDS_FA: tuple[tuple[str, str], ...] = (
    ("month", "ماه"),
    ("year", "سال"),
    ("quarter", "فصل"),
    ("week", "هفته"),
    ("day", "روز"),
    ("date", "تاریخ"),
)

#: Column-name stems for foreign keys that point at nothing (decoys).
DECOY_STEMS: tuple[str, ...] = ("Batch", "Version", "Import", "Audit", "Session", "Trace")

#: Column-name stems used by "legacy" foreign keys that only an explicit
#: relationship can explain (``Cust_Ref``): suffixes that the naming-convention
#: rule does not recognise.
LEGACY_SUFFIXES: tuple[str, ...] = ("Ref", "Link", "Fk", "Code")
