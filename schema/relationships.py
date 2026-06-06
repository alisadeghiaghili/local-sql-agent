"""Foreign-key relationships between Auction tables.

Injected into the prompt so the model knows how to JOIN correctly.
"""

from __future__ import annotations

RELATIONSHIPS: str = """
[Auction_Fact].[Contract].Ring_ID                   = [Auction_Dim].[Ring].ID
[Auction_Fact].[Contract].Date_ID                   = [general_Dim].[Date].ID
[Auction_Fact].[Contract].Supplier_ID               = [Auction_Dim].[Supplier].ID
[Auction_Fact].[Contract].BuyerBroker_ID            = [Auction_Dim].[Broker].ID
[Auction_Fact].[Contract].SellerBroker_ID           = [Auction_Dim].[Broker].ID
[Auction_Fact].[Contract].Symbol_ID                 = [Auction_Dim].[Symbol].ID
[Auction_Fact].[Contract].ContractKind_ID           = [Auction_Dim].[ContractKind].ID
[Auction_Fact].[Contract].ContractStatus_ID         = [Auction_Dim].[ContractStatus].ID

[Auction_Fact].[CustomerContract].BuyerCustomer_ID  = [Auction_Dim].[Customer].ID
[Auction_Fact].[CustomerContract].Contract_ID       = [Auction_Fact].[Contract].ID
[Auction_Fact].[CustomerContract].Date_ID           = [general_Dim].[Date].ID
[Auction_Fact].[CustomerContract].Ring_ID           = [Auction_Dim].[Ring].ID
[Auction_Fact].[CustomerContract].Supplier_ID       = [Auction_Dim].[Supplier].ID
[Auction_Fact].[CustomerContract].BuyerBroker_ID    = [Auction_Dim].[Broker].ID
[Auction_Fact].[CustomerContract].SellerBroker_ID   = [Auction_Dim].[Broker].ID
[Auction_Fact].[CustomerContract].Symbol_ID         = [Auction_Dim].[Symbol].ID

[Auction_Fact].[Offer].Date_ID                      = [general_Dim].[Date].ID
[Auction_Fact].[Offer].Ring_ID                      = [Auction_Dim].[Ring].ID
[Auction_Fact].[Offer].Supplier_ID                  = [Auction_Dim].[Supplier].ID
[Auction_Fact].[Offer].Broker_ID                    = [Auction_Dim].[Broker].ID
[Auction_Fact].[Offer].Symbol_ID                    = [Auction_Dim].[Symbol].ID

[Auction_Fact].[Order].BuyerCustomer_ID             = [Auction_Dim].[Customer].ID
[Auction_Fact].[Order].BuyerBroker_ID               = [Auction_Dim].[Broker].ID
[Auction_Fact].[Order].Ring_ID                      = [Auction_Dim].[Ring].ID
[Auction_Fact].[Order].Symbol_ID                    = [Auction_Dim].[Symbol].ID
[Auction_Fact].[Order].Offer_ID                     = [Auction_Fact].[Offer].ID
"""
