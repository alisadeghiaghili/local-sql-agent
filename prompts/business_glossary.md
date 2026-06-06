# BUSINESS GLOSSARY

---

## مشتری / Customer

هر شخص یا سازمانی که در بورس کالا خرید می‌کند.

در جدول: `[Auction_Dim].[Customer]`

---

## قرارداد / Contract

یک معامله بسته‌شده در تالار.

جدول اصلی: `[Auction_Fact].[Contract]`
جزئیات خرید: `[Auction_Fact].[CustomerContract]`

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
