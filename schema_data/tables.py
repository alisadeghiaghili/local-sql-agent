TABLES = {

    "Customer": {
        "table": "[Auction_Dim].[Customer]",
        "description": """
مشتری / خریدار / مشتریان / اشخاص / افراد
Customer / Buyer / Purchase Customer
        """
    },

    "Broker": {
        "table": "[Auction_Dim].[Broker]",
        "description": """
کارگزار / شرکت کارگزاری / نماینده معامله
Broker / Trading Broker
        """
    },

    "Supplier": {
        "table": "[Auction_Dim].[Supplier]",
        "description": """
عرضه کننده / فروشنده / تامین کننده
Supplier / Seller / Vendor
        """
    },

    "Ring": {
        "table": "[Auction_Dim].[Ring]",
        "description": """
رینگ / تالار / بازار
Ring / Trading Ring / Trading Hall
        """
    },

    "Symbol": {
        "table": "[Auction_Dim].[Symbol]",
        "description": """
نماد / کالا / محصول / کد کالا / نماد معاملاتی / تولیدکننده / گروه کالا / زیرگروه کالا
Commodity / Product / Symbol / Trading Symbol / Producer
        """
    },

    "Date": {
        "table": "[General_Dim].[Date]",
        "description": """
تاریخ / زمان / سال / ماه / فصل / هفته / روز / روز هفته / تاریخ شمسی / تاریخ میلادی
Date / Time / Year / Month / Season / Week / Day
        """
    },

    "Contract": {
        "table": "[Auction_Fact].[Contract]",
        "description": """
قرارداد / معامله / معاملات / فروش / ارزش معامله / تعداد قرارداد / حجم معامله / قیمت معامله
Contract / Trade / Transaction / Sales / Deal
        """
    },

    "CustomerContract": {
        "table": "[Auction_Fact].[CustomerContract]",
        "description": """
خرید مشتری / معامله مشتری / ارزش خرید / حجم خرید / خریدار / مشتری خریدار
Purchase / Customer Purchase / Customer Contract / Buyer / Customer Trade
        """
    },

    "Offer": {
        "table": "[Auction_Fact].[Offer]",
        "description": """
عرضه / عرضه کالا / حجم عرضه / ارزش عرضه / مقدار عرضه / قیمت عرضه / عرضه کننده
Offer / Commodity Offer / Supply / Supply Volume / Supply Value
        """
    },

    "Order": {
        "table": "[Auction_Fact].[Order]",
        "description": """
سفارش / درخواست خرید / ثبت سفارش
Order / Purchase Request
        """
    },

    "Bank": {
        "table": "[Auction_Dim].[Bank]",
        "description": """
بانک / شعبه
Bank / Branch
        """
    },

    "Carrier": {
        "table": "[Auction_Dim].[Carrier]",
        "description": """
حمل کننده / شرکت حمل
Carrier / Transport Company
        """
    },

    "ContractKind": {
        "table": "[Auction_Dim].[ContractKind]",
        "description": """
نوع قرارداد / قرارداد نقدی / قرارداد سلف
Contract Type
        """
    },

    "ContractStatus": {
        "table": "[Auction_Dim].[ContractStatus]",
        "description": """
وضعیت قرارداد
Contract Status
        """
    },

    "Currency": {
        "table": "[Auction_Dim].[Currency]",
        "description": """
ارز
Currency / Exchange Currency
        """
    },

    "DeliveryPlace": {
        "table": "[Auction_Dim].[DeliveryPlace]",
        "description": """
محل تحویل / انبار
Warehouse / Delivery Place
        """
    },

    "OfferStatus": {
        "table": "[Auction_Dim].[OfferStatus]",
        "description": """
وضعیت عرضه
Offer Status
        """
    },

    "OfferKind": {
        "table": "[Auction_Dim].[OfferKind]",
        "description": """
نوع عرضه
Offer Type
        """
    },

    "PaymentDelivery": {
        "table": "[Auction_Dim].[PaymentDelivery]",
        "description": """
شرایط پرداخت / شرایط تحویل
Payment Terms / Delivery Terms
        """
    },

    "TalarLog": {
        "table": "[Auction_Fact].[TalarLog]",
        "description": """
لاگ تالار / گزارش عملیات / ثبت رویداد
Audit Log / Trading Log
        """
    },

    "ActionType": {
        "table": "[Auction_Dim].[ActionType]",
        "description": """
نوع عملیات / نوع اقدام / اکشن
Action Type / Operation Type
        """
    },

    "BuyMethod": {
        "table": "[Auction_Dim].[BuyMethod]",
        "description": """
روش خرید / شیوه خرید / نوع خرید
Buy Method / Purchase Method
        """
    },

    "ClearingKind": {
        "table": "[Auction_Dim].[ClearingKind]",
        "description": """
نوع تسویه / روش تسویه
Clearing Type / Settlement Type
        """
    },

    "GeneralStatus": {
        "table": "[Auction_Dim].[GeneralStatus]",
        "description": """
وضعیت عمومی / وضعیت
Status / General Status
        """
    },

    "HallMatchingDeliveryKind": {
        "table": "[Auction_Dim].[HallMatchingDeliveryKind]",
        "description": """
نوع تحویل مچینگ / نوع تحویل
Delivery Type / Matching Delivery Type
        """
    },

    "OfferItemStatus": {
        "table": "[Auction_Dim].[OfferItemStatus]",
        "description": """
وضعیت آیتم عرضه / وضعیت کالا
Offer Item Status / Item Status
        """
    },

    "Packet": {
        "table": "[Auction_Dim].[Packet]",
        "description": """
بسته / پکیج / گروه کالا
Packet / Package / Bundle
        """
    },

    "TempCustomer": {
        "table": "[Auction_Dim].[TempCustomer]",
        "description": """
مشتری موقت / مشتری قدیمی
Temporary Customer / Legacy Customer
        """
    },

    "TradeCreditTypes": {
        "table": "[Auction_Dim].[TradeCreditTypes]",
        "description": """
نوع اعتبار معاملاتی / اعتبار خرید / اعتبار معامله
Trade Credit / Credit Type
        """
    }
}
