# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
RELATIONSHIPS = {

    # Contract joins
    "Contract -> Date": "JOIN [General_Dim].[Date] d ON c.Date_ID = d.ID",
    "Contract -> Ring": "JOIN [Auction_Dim].[Ring] r ON c.Ring_ID = r.ID",
    "Contract -> Symbol": "JOIN [Auction_Dim].[Symbol] s ON c.Symbol_ID = s.ID",
    "Contract -> Supplier": "JOIN [Auction_Dim].[Supplier] sup ON c.Supplier_ID = sup.ID",
    "Contract -> BuyerBroker": "JOIN [Auction_Dim].[Broker] bb ON c.BuyerBroker_ID = bb.ID",
    "Contract -> SellerBroker": "JOIN [Auction_Dim].[Broker] sb ON c.SellerBroker_ID = sb.ID",
    "Contract -> ContractKind": "JOIN [Auction_Dim].[ContractKind] ck ON c.ContractKind_ID = ck.ID",
    "Contract -> ContractStatus": "JOIN [Auction_Dim].[ContractStatus] cs ON c.ContractStatus_ID = cs.ID",

    # CustomerContract joins
    "CustomerContract -> Date": "JOIN [General_Dim].[Date] d ON cc.Date_ID = d.ID",
    "CustomerContract -> Ring": "JOIN [Auction_Dim].[Ring] r ON cc.Ring_ID = r.ID",
    "CustomerContract -> Symbol": "JOIN [Auction_Dim].[Symbol] s ON cc.Symbol_ID = s.ID",
    "CustomerContract -> Customer": "JOIN [Auction_Dim].[Customer] c ON cc.BuyerCustomer_ID = c.ID",
    "CustomerContract -> BuyerBroker": "JOIN [Auction_Dim].[Broker] bb ON cc.BuyerBroker_ID = bb.ID",
    "CustomerContract -> SellerBroker": "JOIN [Auction_Dim].[Broker] sb ON cc.SellerBroker_ID = sb.ID",

    # Offer joins
    "Offer -> Date": "JOIN [General_Dim].[Date] d ON o.Date_ID = d.ID",
    "Offer -> Ring": "JOIN [Auction_Dim].[Ring] r ON o.Ring_ID = r.ID",
    "Offer -> Symbol": "JOIN [Auction_Dim].[Symbol] s ON o.Symbol_ID = s.ID",
    "Offer -> Supplier": "JOIN [Auction_Dim].[Supplier] sup ON o.Supplier_ID = sup.ID",
    "Offer -> SellerBroker": "JOIN [Auction_Dim].[Broker] sb ON o.SellerBroker_ID = sb.ID",
    "Offer -> OfferStatus": "JOIN [Auction_Dim].[OfferStatus] os ON o.OfferStatus_ID = os.ID",

    # Order joins
    "Order -> Date": "JOIN [General_Dim].[Date] d ON ord.Date_ID = d.ID",
    "Order -> Ring": "JOIN [Auction_Dim].[Ring] r ON ord.Ring_ID = r.ID",
    "Order -> Symbol": "JOIN [Auction_Dim].[Symbol] s ON ord.Symbol_ID = s.ID",
    "Order -> Customer": "JOIN [Auction_Dim].[Customer] c ON ord.BuyerCustomer_ID = c.ID",
    "Order -> BuyerBroker": "JOIN [Auction_Dim].[Broker] b ON ord.BuyerBroker_ID = b.ID",
}
