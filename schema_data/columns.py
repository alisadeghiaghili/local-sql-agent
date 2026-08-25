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
        "NationalID": "National ID / national code (کد ملی، شناسه ملی). Exact column name is NationalID — never write NationalCode.",
        "IsActive": "Active flag",
    },

    "Supplier": {
        "ID": "Primary key",
        "Customer_Name": "Supplier name",
        "NationalID": "National ID / national code (کد ملی، شناسه ملی). Exact column name is NationalID — never write NationalCode.",
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
        "RealDate": "Gregorian (میلادی) calendar date (DATE type). This is the Gregorian date column — there is NO column named GregorianDate. Use for Gregorian date filters/display.",
        "PersianDate": "Full Shamsi (Persian) date as a zero-padded string 'YYYY/MM/DD' (e.g. 1405/01/01). Preferred way to filter dates: exact day WHERE d.PersianDate = '1405/01/01'; year prefix WHERE d.PersianDate LIKE '1405/%'; year+month prefix WHERE d.PersianDate LIKE '1405/05/%'.",
        "PersianYear": "Shamsi year number, 4-digit integer (e.g. 1402). Use for year filters: WHERE d.PersianYear = 1402.",
        "PersianSeason": "Shamsi season number 1-4 (1=بهار spring, 2=تابستان summer, 3=پاییز autumn, 4=زمستان winter)",
        "PersianSeasonName": "Shamsi season name in Persian (بهار، تابستان، پاییز، زمستان)",
        "PersianMonth": "Shamsi month number 1-12 (1=فروردین ... 12=اسفند). Use for month filters: WHERE d.PersianMonth = 5.",
        "PersianMonthName": "Shamsi month name in Persian (فروردین، اردیبهشت، خرداد، تیر، مرداد، شهریور، مهر، آبان، آذر، دی، بهمن، اسفند)",
        "PersianDayOfMonth": "Shamsi day-of-month number 1-31. This is the day-of-month column — there is NO column named PersianDay. Use for day filters: WHERE d.PersianDayOfMonth = 15.",
        "PersianWeekOfYear": "Shamsi week number within the year (1-53). The Persian week runs Saturday (شنبه) to Friday (جمعه).",
        "PersianWeekRange": "Week date range as string 'YYYY/MM/DD - YYYY/MM/DD' (e.g. 1402/01/01 - 1402/01/07). Use for week-range filters/display.",
        "PersianDayOfWeek": "Shamsi day-of-week number 1-7, where 1=شنبه (Saturday, first day of the Persian week) and 7=جمعه (Friday). Most reliable column for day-of-week filters: WHERE d.PersianDayOfWeek = 5.",
        "PersianDayOfWeekName": "Shamsi day-of-week name in Persian (شنبه، یکشنبه، دوشنبه، سه‌شنبه، چهارشنبه، پنجشنبه، جمعه). CAUTION: stored inconsistently in the DB (e.g. 'چهار شنبه' vs 'چهارشنبه') — prefer PersianDayOfWeek (integer) for filtering; use this column only for display.",
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
