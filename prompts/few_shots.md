Question:
How many customers exist?

SQL:
SELECT COUNT(*) AS CustomerCount
FROM [Auction_Dim].[Customer]


Question:
How many contracts exist?

SQL:
SELECT COUNT(*) AS ContractCount
FROM [Auction_Fact].[Contract]


Question:
Which ring has the highest sales?

SQL:
SELECT TOP 1
    r.Name,
    SUM(cc.TotalPrice) AS TotalSales
FROM [Auction_Fact].[CustomerContract] cc
JOIN [Auction_Dim].[Ring] r
    ON cc.Ring_ID = r.ID
GROUP BY r.Name
ORDER BY TotalSales DESC


Question:
Show distinct Persian month names.

SQL:
SELECT DISTINCT
    d.PersianMonthName
FROM [General_Dim].[Date] d


Question:
Top 5 customers by purchase value.

SQL:
SELECT TOP 5
    c.Name,
    SUM(cc.TotalPrice) AS PurchaseValue
FROM [Auction_Fact].[CustomerContract] cc
JOIN [Auction_Dim].[Customer] c
    ON cc.BuyerCustomer_ID = c.ID
GROUP BY c.Name
ORDER BY PurchaseValue DESC
