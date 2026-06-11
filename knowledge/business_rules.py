BUSINESS_RULES = {

    "purchase": """
    Customer purchases must use Auction_Fact.CustomerContract.
    Purchase value = SUM(CustomerContract.TotalPrice)
    Purchase volume = SUM(CustomerContract.Quantity)
    Customer ranking must be based on CustomerContract.
    """,

    "trade": """
    Trade analysis must use Auction_Fact.Contract.
    Contract value = SUM(Contract.TotalPrice)
    Contract volume = SUM(Contract.HallMatchingQuantity)
    Trade count = COUNT(DISTINCT Contract.ID)
    Average trade price = AVG(Contract.Price)
    """,

    "offer": """
    Offer analysis must use Auction_Fact.Offer.
    Offer quantity = SUM(Offer.OfferQuantity)
    Offer value = SUM(Offer.HallContractTotalPrice)
    Offer count = COUNT(DISTINCT Offer.ID)
    """,

    "order": """
    Order analysis must use Auction_Fact.Order.
    Order volume = SUM(Order.BuyOrder_Lot)
    Order count = COUNT(DISTINCT Order.ID)
    """,

    "customer": """
    مشتری = Customer
    خریدار = Customer
    Customer ranking must use CustomerContract.
    """,

    "supplier": """
    عرضه کننده = Supplier
    فروشنده = Supplier
    تامین کننده = Supplier
    """,

    "broker": """
    کارگزار = Broker
    شرکت کارگزاری = Broker

    Broker analysis must use Auction_Dim.Broker.

    IMPORTANT:

    When user asks about کارگزار خریدار / Buyer Broker:
      Use: BuyerBroker_ID
      Join: Fact.BuyerBroker_ID = Broker.ID

    When user asks about کارگزار فروشنده / Seller Broker:
      Use: SellerBroker_ID
      Join: Fact.SellerBroker_ID = Broker.ID

    Buyer broker means BuyerBroker_ID.
    Seller broker means SellerBroker_ID.
    Never use generic Broker_ID when the question explicitly mentions
    کارگزار خریدار or کارگزار فروشنده.
    """,

    "symbol": """
    نماد = Symbol
    کالا = Symbol
    محصول = Symbol
    Trading symbol = TradingSymbol
    """,

    "date": """
    All date filtering must use General_Dim.Date.
    Year filter = PersianYear
    Month filter = PersianMonth
    Persian month name filter = PersianMonthName
    """,

    "ring": """
    تالار = Ring
    رینگ = Ring
    پتروشیمی = تالار پتروشیمی
    سیمان = تالار سیمان
    """,

    "currency": """
    ارز = Currency
    Currency name comes from Currency.PersianName
    """,

    "bank": """
    بانک = Bank
    شعبه بانک = Bank
    """,

    "carrier": """
    حمل کننده = Carrier
    باربری = Carrier
    """,

    "delivery": """
    محل تحویل = DeliveryPlace
    انبار = DeliveryPlace
    """,

    "payment": """
    شرایط پرداخت = PaymentDelivery
    شرایط تحویل = PaymentDelivery
    """,

    "topn": """
    Top customers:
    ORDER BY SUM(CustomerContract.TotalPrice) DESC

    Top suppliers:
    ORDER BY SUM(Offer.HallContractTotalPrice) DESC

    Top commodities:
    ORDER BY SUM(CustomerContract.TotalPrice) DESC
    """
}
