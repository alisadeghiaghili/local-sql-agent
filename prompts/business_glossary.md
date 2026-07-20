# BUSINESS GLOSSARY

---

## مشتری / Customer

هر شخص یا سازمانی که در بورس کالا خرید می‌کند.

در جدول: `[Auction_Dim].[Customer]`

---

## قرارداد / Contract

در این دیتابیس منظور از "معامله" و "خرید" مشتری است که در
`[Auction_Fact].[CustomerContract]` ثبت می‌شود.
`[Auction_Fact].[Contract]` فقط برای تطابق تالاری (Hall Matching)
استفاده می‌شود و ربطی به معاملات نهایی ندارد.

جدول اصلی معاملات: `[Auction_Fact].[CustomerContract]`
جدول تطابق تالار: `[Auction_Fact].[Contract]`

---

## رینگ / Ring

تالار معاملاتی. هر رینگ یک بازار تخصصی است.

در جدول: `[Auction_Dim].[Ring]`

---

## عرضه‌کننده / Supplier

فروشنده، تامین‌کننده کالا.

در جدول: `[Auction_Dim].[Supplier]`

---

## کارگزار / Broker

شرکت کارگزاری که معاملات را انجام می‌دهد.

در جدول: `[Auction_Dim].[Broker]`

ستون نام: `PersianName` — نام کارگزاری (Broker name)

⚠️ Broker ستون `Name` ندارد. همیشه از `PersianName` استفاده کنید.
نادرست: `b.Name`
درست: `b.PersianName`

---

## نماد / Symbol

کالا یا محصول قابل معامله.

در جدول: `[Auction_Dim].[Symbol]`

---

## تاریخ / Date

تقویم شمسی کامل.

در جدول: `[general_Dim].[Date]`

---

# QUERY INTERPRETATION RULES

اگر کاربر از عبارت "بیشترین خرید" استفاده کرد:
ORDER BY SUM(TotalPrice) DESC

اگر کاربر از عبارت "کمترین خرید" استفاده کرد:
ORDER BY SUM(TotalPrice) ASC

پنج مشتری برتر: TOP 5
ده مشتری برتر: TOP 10

اگر کاربر سال یا ماه را مشخص نکرد:
از جدیدترین سال و ماه موجود استفاده شود.
