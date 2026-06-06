"""TABLES registry: maps logical name -> SQL table + Persian/English description.

Used by retriever.py to select relevant tables per question.
"""

from __future__ import annotations

TABLES: dict[str, dict] = {
    "Customer": {
        "table": "[Auction_Dim].[Customer]",
        "description": "مشتری \nخریدار \nمشتریان \nاشخاص \nافراد \nCustomer \nBuyer \nPurchase Customer",
    },
    "Broker": {
        "table": "[Auction_Dim].[Broker]",
        "description": "کارگزار \nشرکت کارگزاری \nنماینده معامله \nBroker \nTrading Broker",
    },
    "Supplier": {
        "table": "[Auction_Dim].[Supplier]",
        "description": "عرضه کننده \nفروشنده \nتامین کننده \nSupplier \nSeller \nVendor",
    },
    "Ring": {
        "table": "[Auction_Dim].[Ring]",
        "description": "رینگ \nتالار \nبازار \nRing \nTrading Ring \nTrading Hall",
    },
    "Symbol": {
        "table": "[Auction_Dim].[Symbol]",
        "description": "نماد \nکالا \nمحصول \nکد کالا \nتولیدکننده \nCommodity \nProduct \nSymbol \nProducer",
    },
    "Date": {
        "table": "[General_Dim].[Date]",
        "description": "تاریخ \nسال \nماه \nفصل \nهفته \nروز \nتاریخ شمسی \nDate \nYear \nMonth \nSeason",
    },
    "Contract": {
        "table": "[Auction_Fact].[Contract]",
        "description": "قرارداد \nمعامله \nفروش \nحجم معامله \nContract \nTrade \nSales \nDeal",
    },
    "CustomerContract": {
        "table": "[Auction_Fact].[CustomerContract]",
        "description": "خرید مشتری \nمعامله مشتری \nارزش خرید \nPurchase \nCustomer Purchase \nCustomer Contract \nBuyer",
    },
    "Offer": {
        "table": "[Auction_Fact].[Offer]",
        "description": "عرضه \nعرضه کالا \nحجم عرضه \nOffer \nCommodity Offer \nSupply",
    },
    "Order": {
        "table": "[Auction_Fact].[Order]",
        "description": "سفارش \nدرخواست خرید \nOrder \nPurchase Request",
    },
    "Bank": {
        "table": "[Auction_Dim].[Bank]",
        "description": "بانک \nشعبه \nBank \nBranch",
    },
    "Carrier": {
        "table": "[Auction_Dim].[Carrier]",
        "description": "حمل کننده \nشرکت حمل \nCarrier \nTransport",
    },
    "ContractKind": {
        "table": "[Auction_Dim].[ContractKind]",
        "description": "نوع قرارداد \nقرارداد نقدی \nContract Type",
    },
    "ContractStatus": {
        "table": "[Auction_Dim].[ContractStatus]",
        "description": "وضعیت قرارداد \nContract Status",
    },
    "Currency": {
        "table": "[Auction_Dim].[Currency]",
        "description": "ارز \nCurrency",
    },
    "DeliveryPlace": {
        "table": "[Auction_Dim].[DeliveryPlace]",
        "description": "محل تحویل \nانبار \nWarehouse \nDelivery Place",
    },
    "OfferStatus": {
        "table": "[Auction_Dim].[OfferStatus]",
        "description": "وضعیت عرضه \nOffer Status",
    },
    "OfferKind": {
        "table": "[Auction_Dim].[OfferKind]",
        "description": "نوع عرضه \nOffer Type",
    },
    "PaymentDelivery": {
        "table": "[Auction_Dim].[PaymentDelivery]",
        "description": "شرایط پرداخت \nشرایط تحویل \nPayment Terms \nDelivery Terms",
    },
    "TalarLog": {
        "table": "[Auction_Fact].[TalarLog]",
        "description": "لاگ تالار \nگزارش عملیات \nAudit Log \nTrading Log",
    },
    "ActionType": {
        "table": "[Auction_Dim].[ActionType]",
        "description": "نوع عملیات \nنوع اقدام \nAction Type",
    },
    "BuyMethod": {
        "table": "[Auction_Dim].[BuyMethod]",
        "description": "روش خرید \nشیوه خرید \nBuy Method",
    },
    "ClearingKind": {
        "table": "[Auction_Dim].[ClearingKind]",
        "description": "نوع تسویه \nروش تسویه \nClearing Type",
    },
    "GeneralStatus": {
        "table": "[Auction_Dim].[GeneralStatus]",
        "description": "وضعیت عمومی \nStatus \nGeneral Status",
    },
    "HallMatchingDeliveryKind": {
        "table": "[Auction_Dim].[HallMatchingDeliveryKind]",
        "description": "نوع تحویل مچینگ \nDelivery Type \nMatching Delivery",
    },
    "OfferItemStatus": {
        "table": "[Auction_Dim].[OfferItemStatus]",
        "description": "وضعیت آیتم عرضه \nOffer Item Status",
    },
    "Packet": {
        "table": "[Auction_Dim].[Packet]",
        "description": "بسته \nپکیج \nPacket \nPackage",
    },
    "TempCustomer": {
        "table": "[Auction_Dim].[TempCustomer]",
        "description": "مشتری موقت \nمشتری قدیمی \nTemporary Customer \nLegacy Customer",
    },
    "TradeCreditTypes": {
        "table": "[Auction_Dim].[TradeCreditTypes]",
        "description": "نوع اعتبار معاملاتی \nاعتبار خرید \nTrade Credit",
    },
}
