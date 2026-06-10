EXAMPLES = [

    {
        "tags": [
            "customer",
            "count"
        ],

        "question":
            "How many customers exist?",

        "sql":
            """
            SELECT COUNT(*) AS CustomerCount
            FROM [Auction_Dim].[Customer]
            """
    },

    {
        "tags": [
            "contract",
            "count"
        ],

        "question":
            "How many contracts exist?",

        "sql":
            """
            SELECT COUNT(*) AS ContractCount
            FROM [Auction_Fact].[Contract]
            """
    },

    {
        "tags": [
            "customer_contract",
            "count"
        ],

        "question":
            "How many customer contracts exist?",

        "sql":
            """
            SELECT COUNT(*) AS CustomerContractCount
            FROM [Auction_Fact].[CustomerContract]
            """
    },

    {
        "tags": [
            "supplier",
            "count"
        ],

        "question":
            "How many suppliers exist?",

        "sql":
            """
            SELECT COUNT(*) AS SupplierCount
            FROM [Auction_Dim].[Supplier]
            """
    },

    {
        "tags": [
            "symbol",
            "count"
        ],

        "question":
            "How many symbols exist?",

        "sql":
            """
            SELECT COUNT(*) AS SymbolCount
            FROM [Auction_Dim].[Symbol]
            """
    },

    {
        "tags": [
            "trade",
            "value",
            "sum"
        ],

        "question":
            "What is the total trade value?",

        "sql":
            """
            SELECT SUM(TotalPrice) AS TradeValue
            FROM [Auction_Fact].[Contract]
            """
    },

    {
        "tags": [
            "purchase",
            "value",
            "sum"
        ],

        "question":
            "What is the total purchase value?",

        "sql":
            """
            SELECT SUM(TotalPrice) AS PurchaseValue
            FROM [Auction_Fact].[CustomerContract]
            """
    },

    {
        "tags": [
            "purchase",
            "volume",
            "sum"
        ],

        "question":
            "What is the total purchase volume?",

        "sql":
            """
            SELECT SUM(Quantity) AS PurchaseVolume
            FROM [Auction_Fact].[CustomerContract]
            """
    },

    {
        "tags": [
            "trade",
            "price",
            "average"
        ],

        "question":
            "What is the average trade price?",

        "sql":
            """
            SELECT AVG(Price) AS AvgTradePrice
            FROM [Auction_Fact].[Contract]
            """
    },

    {
        "tags": [
            "ring",
            "trade",
            "value",
            "top"
        ],

        "question":
            "Which ring has the highest sales?",

        "sql":
            """
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
        "tags": [
            "customer",
            "top",
            "purchase",
            "value"
        ],

        "question":
            "Top 5 customers by purchase value.",

        "sql":
            """
            SELECT TOP 5
                c.Name,
                SUM(cc.TotalPrice) AS PurchaseValue
            FROM [Auction_Fact].[CustomerContract] cc
            JOIN [Auction_Dim].[Customer] c
                ON cc.BuyerCustomer_ID = c.ID
            GROUP BY c.Name
            ORDER BY PurchaseValue DESC
            """
    },

    {
        "tags": [
            "supplier",
            "top",
            "trade",
            "value"
        ],

        "question":
            "Top 10 suppliers by trade value.",

        "sql":
            """
            ...
            """
    },

    {
        "tags": [
            "symbol",
            "top",
            "trade",
            "value"
        ],

        "question":
            "Top 10 symbols by trade value.",

        "sql":
            """
            ...
            """
    },

    {
        "tags": [
            "date",
            "month",
            "distinct"
        ],

        "question":
            "Show distinct Persian month names.",

        "sql":
            """
            ...
            """
    },

    {
        "tags": [
            "trade",
            "date",
            "year",
            "group_by"
        ],

        "question":
            "Trade value by Persian year.",

        "sql":
            """
            ...
            """
    },

    {
        "tags": [
            "trade",
            "date",
            "month",
            "group_by"
        ],

        "question":
            "Trade value by month.",

        "sql":
            """
            ...
            """
    },

    {
        "tags": [
            "broker",
            "top",
            "purchase",
            "value"
        ],

        "question":
            "Which broker has the highest purchase value?",

        "sql":
            """
            ...
            """
    },

    {
        "tags": [
            "customer",
            "active",
            "count"
        ],

        "question":
            "How many active customers exist?",

        "sql":
            """
            ...
            """
    },

    {
        "tags": [
            "wage",
            "broker",
            "sum"
        ],

        "question":
            "What is the total broker wage?",

        "sql":
            """
            ...
            """
    },

    {
        "tags": [
            "wage",
            "ime",
            "sum"
        ],

        "question":
            "What is the total IME wage?",

        "sql":
            """
            ...
            """
    },

    {
        "tags": [
            "trade",
            "date",
            "day",
            "top",
            "value"
        ],

        "question":
            "Top 10 days by trade value.",

        "sql":
            """
            ...
            """
    },

    {
        "tags": [
            "buyer",
            "customer",
            "distinct",
            "count"
        ],

        "question":
            "How many unique buyers exist?",

        "sql":
            """
            ...
            """
    }

]