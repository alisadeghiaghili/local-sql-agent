EXAMPLES = [

    {
        "tags": ["customer", "count"],
        "question": "How many customers exist?",
        "sql": """
SELECT COUNT(*) AS CustomerCount
FROM [Auction_Dim].[Customer]
"""
    },

    {
        "tags": ["contract", "trade", "count"],
        "question": "How many contracts exist?",
        "sql": """
SELECT COUNT(*) AS ContractCount
FROM [Auction_Fact].[Contract]
"""
    },

    {
        "tags": ["purchase", "count"],
        "question": "How many customer contracts exist?",
        "sql": """
SELECT COUNT(*) AS CustomerContractCount
FROM [Auction_Fact].[CustomerContract]
"""
    },

    {
        "tags": ["supplier", "count"],
        "question": "How many suppliers exist?",
        "sql": """
SELECT COUNT(*) AS SupplierCount
FROM [Auction_Dim].[Supplier]
"""
    },

    {
        "tags": ["symbol", "count"],
        "question": "How many symbols exist?",
        "sql": """
SELECT COUNT(*) AS SymbolCount
FROM [Auction_Dim].[Symbol]
"""
    },

    {
        "tags": ["trade", "value", "sum"],
        "question": "What is the total trade value?",
        "sql": """
SELECT SUM(c.TotalPrice) AS TradeValue
FROM [Auction_Fact].[Contract] c
"""
    },

    {
        "tags": ["purchase", "value", "sum"],
        "question": "What is the total purchase value?",
        "sql": """
SELECT SUM(cc.TotalPrice) AS PurchaseValue
FROM [Auction_Fact].[CustomerContract] cc
"""
    },

    {
        "tags": ["purchase", "volume", "sum"],
        "question": "What is the total purchase volume?",
        "sql": """
SELECT SUM(cc.Quantity) AS PurchaseVolume
FROM [Auction_Fact].[CustomerContract] cc
"""
    },

    {
        "tags": ["trade", "price", "average"],
        "question": "What is the average trade price?",
        "sql": """
SELECT AVG(c.Price) AS AvgTradePrice
FROM [Auction_Fact].[Contract] c
"""
    },

    {
        "tags": ["ring", "trade", "value", "top"],
        "question": "Which ring has the highest sales?",
        "sql": """
SELECT TOP 1
    r.Name,
    SUM(c.TotalPrice) AS TotalSales
FROM [Auction_Fact].[Contract] c
JOIN [Auction_Dim].[Ring] r
    ON c.Ring_ID = r.ID
GROUP BY r.Name
ORDER BY TotalSales DESC
"""
    },

    {
        "tags": ["customer", "top", "purchase", "value"],
        "question": "Top 5 customers by purchase value.",
        "sql": """
SELECT TOP 5
    cu.Name,
    SUM(cc.TotalPrice) AS PurchaseValue
FROM [Auction_Fact].[CustomerContract] cc
JOIN [Auction_Dim].[Customer] cu
    ON cc.BuyerCustomer_ID = cu.ID
GROUP BY cu.Name
ORDER BY PurchaseValue DESC
"""
    },

    {
        "tags": ["supplier", "top", "trade", "value"],
        "question": "Top 10 suppliers by trade value.",
        "sql": """
SELECT TOP 10
    s.Customer_Name,
    SUM(o.HallContractTotalPrice) AS TradeValue
FROM [Auction_Fact].[Offer] o
JOIN [Auction_Dim].[Supplier] s
    ON o.Supplier_ID = s.ID
GROUP BY s.Customer_Name
ORDER BY TradeValue DESC
"""
    },

    {
        "tags": ["symbol", "top", "trade", "value"],
        "question": "Top 10 symbols by trade value.",
        "sql": """
SELECT TOP 10
    sy.TradingSymbol,
    sy.Commodity_PersianName,
    SUM(cc.TotalPrice) AS PurchaseValue
FROM [Auction_Fact].[CustomerContract] cc
JOIN [Auction_Dim].[Symbol] sy
    ON cc.Symbol_ID = sy.ID
GROUP BY sy.TradingSymbol, sy.Commodity_PersianName
ORDER BY PurchaseValue DESC
"""
    },

    {
        "tags": ["date", "month", "distinct"],
        "question": "Show distinct Persian month names.",
        "sql": """
SELECT DISTINCT d.PersianMonthName
FROM [General_Dim].[Date] d
ORDER BY d.PersianMonthName
"""
    },

    {
        "tags": ["trade", "date", "year", "group_by"],
        "question": "Trade value by Persian year.",
        "sql": """
SELECT
    d.PersianYear,
    SUM(c.TotalPrice) AS TradeValue
FROM [Auction_Fact].[Contract] c
JOIN [General_Dim].[Date] d
    ON c.Date_ID = d.ID
GROUP BY d.PersianYear
ORDER BY d.PersianYear
"""
    },

    {
        "tags": ["trade", "date", "month", "group_by"],
        "question": "Trade value by month.",
        "sql": """
SELECT
    d.PersianYear,
    d.PersianMonth,
    d.PersianMonthName,
    SUM(c.TotalPrice) AS TradeValue
FROM [Auction_Fact].[Contract] c
JOIN [General_Dim].[Date] d
    ON c.Date_ID = d.ID
GROUP BY d.PersianYear, d.PersianMonth, d.PersianMonthName
ORDER BY d.PersianYear, d.PersianMonth
"""
    },

    {
        "tags": ["broker", "top", "purchase", "value"],
        "question": "Which buyer broker has the highest purchase value?",
        "sql": """
SELECT TOP 10
    b.PersianName,
    SUM(cc.TotalPrice) AS PurchaseValue
FROM [Auction_Fact].[CustomerContract] cc
JOIN [Auction_Dim].[Broker] b
    ON cc.BuyerBroker_ID = b.ID
GROUP BY b.PersianName
ORDER BY PurchaseValue DESC
"""
    },

    {
        "tags": ["customer", "active", "count"],
        "question": "How many active customers exist?",
        "sql": """
SELECT COUNT(*) AS ActiveCustomerCount
FROM [Auction_Dim].[Customer]
WHERE IsActive = 1
"""
    },

    {
        "tags": ["wage", "broker", "sum"],
        "question": "What is the total buyer broker wage?",
        "sql": """
SELECT
    b.PersianName,
    SUM(cc.BuyBrokerWage) AS TotalBuyBrokerWage
FROM [Auction_Fact].[CustomerContract] cc
JOIN [Auction_Dim].[Broker] b
    ON cc.BuyerBroker_ID = b.ID
GROUP BY b.PersianName
ORDER BY TotalBuyBrokerWage DESC
"""
    },

    {
        "tags": ["wage", "sum"],
        "question": "What is the total IME wage (buy side)?",
        "sql": """
SELECT SUM(cc.BuyIMEWage) AS TotalBuyIMEWage
FROM [Auction_Fact].[CustomerContract] cc
"""
    },

    {
        "tags": ["trade", "date", "day", "top", "value"],
        "question": "Top 10 days by trade value.",
        "sql": """
SELECT TOP 10
    d.PersianDate,
    SUM(c.TotalPrice) AS TradeValue
FROM [Auction_Fact].[Contract] c
JOIN [General_Dim].[Date] d
    ON c.Date_ID = d.ID
GROUP BY d.PersianDate
ORDER BY TradeValue DESC
"""
    },

    {
        "tags": ["customer", "distinct", "count"],
        "question": "How many unique buyers exist?",
        "sql": """
SELECT COUNT(DISTINCT cc.BuyerCustomer_ID) AS UniqueBuyerCount
FROM [Auction_Fact].[CustomerContract] cc
"""
    },

    {
        "tags": ["offer", "value", "sum", "ring"],
        "question": "Total offer value by ring.",
        "sql": """
SELECT
    r.Name AS RingName,
    SUM(o.HallContractTotalPrice) AS OfferValue
FROM [Auction_Fact].[Offer] o
JOIN [Auction_Dim].[Ring] r
    ON o.Ring_ID = r.ID
GROUP BY r.Name
ORDER BY OfferValue DESC
"""
    },

    {
        "tags": ["trade", "count", "ring"],
        "question": "Number of contracts per ring.",
        "sql": """
SELECT
    r.Name AS RingName,
    COUNT(DISTINCT c.ID) AS ContractCount
FROM [Auction_Fact].[Contract] c
JOIN [Auction_Dim].[Ring] r
    ON c.Ring_ID = r.ID
GROUP BY r.Name
ORDER BY ContractCount DESC
"""
    },

    {
        "tags": ["purchase", "value", "date", "year", "customer", "top"],
        "question": "Top 5 customers by purchase value in a specific year.",
        "sql": """
SELECT TOP 5
    cu.Name,
    SUM(cc.TotalPrice) AS PurchaseValue
FROM [Auction_Fact].[CustomerContract] cc
JOIN [Auction_Dim].[Customer] cu
    ON cc.BuyerCustomer_ID = cu.ID
JOIN [General_Dim].[Date] d
    ON cc.Date_ID = d.ID
WHERE d.PersianYear = 1402
GROUP BY cu.Name
ORDER BY PurchaseValue DESC
"""
    }

]
