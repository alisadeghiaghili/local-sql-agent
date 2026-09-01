# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
TABLE_DESCRIPTIONS = {

    # --- Fact Tables ---
    "Contract": "Auction_Fact.Contract — trade contracts (transactions) معامله قرارداد",
    "CustomerContract": "Auction_Fact.CustomerContract — customer purchase records خرید مشتری",
    "Offer": "Auction_Fact.Offer — supply offers submitted by suppliers عرضه کالا عرضهکننده",
    "Order": "Auction_Fact.Order — purchase orders placed by buyers سفارش خرید",
    "TalarLog": "Auction_Fact.TalarLog — operational log of trading hall events تالار",

    # --- Dimension Tables ---
    "Customer": "Auction_Dim.Customer — buyer / customer master data مشتری خریدار",
    "Supplier": "Auction_Dim.Supplier — supplier / seller master data تامین‌کننده فروشنده",
    "Broker": "Auction_Dim.Broker — brokerage firms کارگزاری columns: PersianName (broker name). PersianName is the display name, NOT Name column (Name does not exist on Broker).",
    "Symbol": "Auction_Dim.Symbol — trading symbols (commodities) نماد کالا",
    "Ring": "Auction_Dim.Ring — trading halls / rings تالار رینگ",
    "Date": "General_Dim.Date — Persian calendar date dimension تاریخ سال ماه",
    "Currency": "Auction_Dim.Currency — currency master data ارز",
    "Bank": "Auction_Dim.Bank — bank master data بانک",
    "Carrier": "Auction_Dim.Carrier — logistics / transport companies حمل‌ونقل",
    "ContractKind": "Auction_Dim.ContractKind — contract type (cash, forward, etc.) نوع قرارداد",
    "ContractStatus": "Auction_Dim.ContractStatus — contract lifecycle status وضعیت قرارداد",
    "OfferStatus": "Auction_Dim.OfferStatus — offer lifecycle status وضعیت عرضه",
    "OfferKind": "Auction_Dim.OfferKind — offer type classification نوع عرضه",
    "DeliveryPlace": "Auction_Dim.DeliveryPlace — delivery location / warehouse محل تحویل",
    "PaymentDelivery": "Auction_Dim.PaymentDelivery — payment & delivery terms شرایط تحویل پرداخت",
    "ClearingKind": "Auction_Dim.ClearingKind — settlement / clearing type نوع تسویه",
    "BuyMethod": "Auction_Dim.BuyMethod — purchase method classification روش خرید",
    "GeneralStatus": "Auction_Dim.GeneralStatus — generic status lookup وضعیت عمومی",
}
