"""TABLE_SCHEMAS: maps table logical name -> column list string.

Used by schema_registry.py to build the schema context injected into the prompt.
"""

from __future__ import annotations

TABLE_SCHEMAS: dict[str, str] = {
    "Customer": """
[Auction_Dim].[Customer]
Columns: ID, Customer_OriginalPK, Name, TypeID, TypeName, NationalID, Broker_OriginalPK, IsActive
""",
    "Broker": """
[Auction_Dim].[Broker]
Columns: ID, Broker_OriginalPK, PersianName, EnglishName, CEO, StatusID, StatusName, CodeE, CodeP, NationalID
""",
    "Supplier": """
[Auction_Dim].[Supplier]
Columns: ID, Supplier_OriginalPK, Customer_OriginalPK, Customer_Name, Customer_TypeID, Customer_TypeDescription, LevelID, LevelDescription, NationalID
""",
    "Ring": """
[Auction_Dim].[Ring]
Columns: ID, Ring_OriginalPK, Name
""",
    "Date": """
[General_Dim].[Date]
Columns: ID, RealDate, PersianDate, PersianYear, PersianSeason, PersianSeasonName, PersianMonth, PersianMonthName, PersianDayOfMonth, PersianWeekOfYear, PersianDayOfWeek, PersianDayOfWeekName
""",
    "Symbol": """
[Auction_Dim].[Symbol]
Columns: ID, Symbol_OriginalPK, TradingSymbol, Commodity_PersianName, Commodity_EnglishName, CommodityMainGroup_PersianName, CommodityGroup_PersianName, CommoditySubGroup_PersianName, Producer_PersianName, Producer_City, Producer_Province, DeliveryPlace_PersianName, Commodity_ShipmentWeight
""",
    "Bank": """
[Auction_Dim].[Bank]
Columns: ID, Name, BranchName, BranchCode
""",
    "Carrier": """
[Auction_Dim].[Carrier]
Columns: ID, Name, CEO, Province, City, BrokerName
""",
    "Currency": """
[Auction_Dim].[Currency]
Columns: ID, PersianName, EnglishName, EqualityRate
""",
    "ContractKind": """
[Auction_Dim].[ContractKind]
Columns: ID, PersianName, EnglishName, KindID, KindDescription, GroupID, GroupDescription
""",
    "ContractStatus": """
[Auction_Dim].[ContractStatus]
Columns: ID, Name
""",
    "OfferStatus": """
[Auction_Dim].[OfferStatus]
Columns: ID, Name
""",
    "OfferKind": """
[Auction_Dim].[OfferKind]
Columns: ID, Name
""",
    "DeliveryPlace": """
[Auction_Dim].[DeliveryPlace]
Columns: ID, PersianName, EnglishName, Symbol
""",
    "PaymentDelivery": """
[Auction_Dim].[PaymentDelivery]
Columns: ID, Name, PaymentKindID, PaymentKindDescription, PaymentCount, DeliveryKindID, DeliveryDescription, DeliveryCount
""",
    "ClearingKind": """
[Auction_Dim].[ClearingKind]
Columns: ID, Name
""",
    "GeneralStatus": """
[Auction_Dim].[GeneralStatus]
Columns: ID, Name, Symbol
""",
    "HallMatchingDeliveryKind": """
[Auction_Dim].[HallMatchingDeliveryKind]
Columns: ID, Name
""",
    "OfferItemStatus": """
[Auction_Dim].[OfferItemStatus]
Columns: ID, Name
""",
    "BuyMethod": """
[Auction_Dim].[BuyMethod]
Columns: ID, PersianName
""",
    "CustomerContract": """
[Auction_Fact].[CustomerContract]
Columns: ID, CustomerContract_OriginalPK, Quantity, TotalPrice, Electronic,
         BuyerCustomer_ID, Contract_ID, Date_ID, Ring_ID, Supplier_ID,
         BuyerBroker_ID, SellerBroker_ID, Symbol_ID,
         ContractKind_ID, Currency_ID, Bank_ID, Carrier_ID, ClearingKind_ID,
         ContractStatus_ID, PaymentStatus_ID, TransferStatus_ID, FinantialStatus_ID,
         BuyBrokerWage, SellBrokerWage, BuyIMEWage, SellIMEWage, PenaltyWage
""",
    "Contract": """
[Auction_Fact].[Contract]
Columns: ID, Contract_OriginalPK, Code, Price, TotalPrice,
         CustomerContractCount, CustomerContractTotalPrice,
         HallMatchingQuantity, HallMatchingWeight, HallMatchingPrice, HallMatchingTotalPrice,
         Date_ID, Ring_ID, Supplier_ID, BuyerBroker_ID, SellerBroker_ID, Symbol_ID,
         ContractKind_ID, Currency_ID, Offer_ID, OfferKind_ID, ContractStatus_ID
""",
    "Offer": """
[Auction_Fact].[Offer]
Columns: ID, Offer_OriginalPK, OfferItem_OriginalPK,
         OfferQuantity, OfferPrice, OfferMaxPrice,
         HallSaleQuantity, HallSaleMinPrice, HallSaleMaxPrice,
         HallPurchaseQuantity, HallPurchaseMinPrice, HallPurchaseMaxPrice,
         HallContractCount, HallContractMinPrice, HallContractAVGPrice, HallContractMaxPrice,
         HallContractTotalQuantity, HallContractTotalPrice,
         ExtraContractCount, ExtraContractMinPrice, ExtraContractAVGPrice, ExtraContractMaxPrice,
         ExtraContractTotalQuantity, ExtraContractTotalPrice,
         LastContractPrice,
         Date_ID, Ring_ID, Supplier_ID, Broker_ID, Symbol_ID,
         ContractKind_ID, Currency_ID,
         OfferStatus_ID, OfferItemStatus_ID, OfferKind_ID,
         PaymentDelivery_ID, DeliveryPlace_ID, BuyMethod_ID,
         CommodityShipmentWeight
""",
    "Order": """
[Auction_Fact].[Order]
Columns: ID, Order_OriginalPK, OrderStatus, OrderDateTime, OrderLot,
         BuyOrder_OriginalPK, BuyOrder_Lot, BuyOrder_Price, BuyOrder_Time,
         Date_ID, OrderBookDate_ID,
         BuyerCustomer_ID, BuyerBroker_ID, SellerBroker_ID,
         Supplier_ID, Symbol_ID, Ring_ID, Offer_ID
""",
    "TalarLog": """
[Auction_Fact].[TalarLog]
Columns: TalarLogID, ContractNumber, GroupID, ActionID,
         UserId, UserName, NewPrice, OldPrice, NewAmount, OldAmount,
         RegisterDateTime, RegisterDate, BrokerID, BrokerName,
         LogType, ActionDescription, RingColor, BranchCity,
         OrderName, OfferShipmentCount
""",
    "ActionType": """
[Auction_Dim].[ActionType]
Columns: ActionID, Title, IsFailed, ActionType
""",
    "Packet": """
[Auction_Dim].[Packet]
Columns: ID, Packet_OriginalPK, Name
""",
    "TempCustomer": """
[Auction_Dim].[TempCustomer]
Columns: CustomerID, PublicCode, NationalCode, CustomerName, CustomerType, CustomerTypeDes, EconomyActivityCode, FK_CustomerID, OldCustomerID
""",
    "TradeCreditTypes": """
[Auction_Dim].[TradeCreditTypes]
Columns: code, CustomerContractDetailNumber, CreditTypeId, Description
""",
}
