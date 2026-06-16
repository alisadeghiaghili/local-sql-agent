TABLE_COLUMNS = {

    "Contract": {
        "ID": "Primary key",
        "Date_ID": "FK → General_Dim.Date",
        "Ring_ID": "FK → Auction_Dim.Ring",
        "Symbol_ID": "FK → Auction_Dim.Symbol",
        "Supplier_ID": "FK → Auction_Dim.Supplier",
        "ContractKind_ID": "FK → Auction_Dim.ContractKind",
        "ContractStatus_ID": "FK → Auction_Dim.ContractStatus",
        "TotalPrice": "Total contract value (Rials)",
        "Price": "Unit price",
        "HallMatchingQuantity": "Matched quantity in trading hall",
        "HallMatchingWeight": "Matched weight",
        "HallMatchingTotalPrice": "Total matched value",
        "BuyerBroker_ID": "FK → Auction_Dim.Broker (buyer side)",
        "SellerBroker_ID": "FK → Auction_Dim.Broker (seller side)",
    },

    "CustomerContract": {
        "ID": "Primary key",
        "Date_ID": "FK → General_Dim.Date",
        "Ring_ID": "FK → Auction_Dim.Ring",
        "Symbol_ID": "FK → Auction_Dim.Symbol",
        "BuyerCustomer_ID": "FK → Auction_Dim.Customer",
        "BuyerBroker_ID": "FK → Auction_Dim.Broker (buyer)",
        "SellerBroker_ID": "FK → Auction_Dim.Broker (seller)",
        "TotalPrice": "Total purchase value (Rials)",
        "Quantity": "Purchased quantity",
        "BuyBrokerWage": "Buyer broker commission",
        "SellBrokerWage": "Seller broker commission",
        "BuyIMEWage": "Exchange fee (buy side)",
        "SellIMEWage": "Exchange fee (sell side)",
        "BuySEOWage": "SEC fee (buy side)",
        "SellSEOWage": "SEC fee (sell side)",
    },

    "Offer": {
        "ID": "Primary key",
        "Date_ID": "FK → General_Dim.Date",
        "Ring_ID": "FK → Auction_Dim.Ring",
        "Symbol_ID": "FK → Auction_Dim.Symbol",
        "Supplier_ID": "FK → Auction_Dim.Supplier",
        "OfferStatus_ID": "FK → Auction_Dim.OfferStatus",
        "OfferKind_ID": "FK → Auction_Dim.OfferKind",
        "OfferQuantity": "Quantity offered",
        "OfferPrice": "Base offer price",
        "OfferMaxPrice": "Maximum offer price",
        "HallSaleQuantity": "Quantity sold in hall",
        "HallPurchaseQuantity": "Demand quantity in hall",
        "HallContractCount": "Number of contracts in hall",
        "HallContractTotalPrice": "Total contract value in hall",
        "SellerBroker_ID": "FK → Auction_Dim.Broker (seller side)",
    },

    "Order": {
        "ID": "Primary key",
        "Date_ID": "FK → General_Dim.Date",
        "Ring_ID": "FK → Auction_Dim.Ring",
        "Symbol_ID": "FK → Auction_Dim.Symbol",
        "BuyerCustomer_ID": "FK → Auction_Dim.Customer",
        "BuyerBroker_ID": "FK → Auction_Dim.Broker",
        "SellerBroker_ID": "FK → Auction_Dim.Broker",
        "BuyOrder_Lot": "Ordered lot size",
    },

    "Customer": {
        "ID": "Primary key",
        "Name": "Customer full name",
        "NationalCode": "National ID",
        "IsActive": "Active flag",
    },

    "Supplier": {
        "ID": "Primary key",
        "Customer_Name": "Supplier name",
        "NationalCode": "National ID",
    },

    "Broker": {
        "ID": "Primary key",
        "PersianName": "Broker name",
        "Code": "Broker code",
    },

    "Symbol": {
        "ID": "Primary key",
        "Commodity_PersianName": "Symbol / commodity name",
        "Commodity_Symbol": "Commodity Symbol",
        "IsActive": "Active flag",
    },

    "Ring": {
        "ID": "Primary key",
        "Name": "Ring / hall name (Persian)",
        "Code": "Ring code",
    },

    "Date": {
        "ID": "Primary key",
        "PersianYear": "Shamsi year (e.g. 1402)",
        "PersianMonth": "Shamsi month number (1-12)",
        "PersianMonthName": "Shamsi month name (e.g. فروردین)",
        "PersianDay": "Shamsi day",
        "GregorianDate": "Gregorian equivalent date",
    },

    "Currency": {
        "ID": "Primary key",
        "PersianName": "Currency name in Persian",
        "Code": "ISO currency code",
    },
    "DeliveryPlace":{
        "DeliveryPlace_OriginalPK": "Primary key",
        "PersianName": "delivery place name in Persian",
    },
}
