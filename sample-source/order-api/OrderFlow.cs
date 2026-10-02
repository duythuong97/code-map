using System;
using System.Data;
using Dapper;
using Microsoft.AspNetCore.Mvc;

namespace Demo.OrderApi;

public record CreateOrderRequest(long CustomerId, string RequestKey);
public record OrderDto(long OrderId, string Status);

[ApiController]
[Route(BaseRoute)]
public class OrderController : ControllerBase
{
    private const string BaseRoute = "api/orders";
    private const string RetryRoute = "retry";
    private const string ReadRoute = "{id}";

    private readonly OrderService _service;

    public OrderController(OrderService service)
    {
        _service = service;
    }

    [HttpPost]
    public IActionResult Create([FromBody] CreateOrderRequest request)
    {
        var order = _service.CreateOrder(request);
        return Ok(order);
    }

    // Idempotency: existing request key returns prior order.
    [HttpPost(RetryRoute)]
    // HttpPost action calls OrderService.CreateOrder
    public IActionResult Retry([FromBody] CreateOrderRequest request)
    {
        var order = _service.CreateOrder(request);
        return Ok(order);
    }

    [HttpGet(ReadRoute)]
    public IActionResult Read(long id) => Ok(_service.ReadOrder(id));
}

[ApiController]
[Route("api/[controller]")]
public class TokenizedController : ControllerBase
{
    [HttpGet("[action]")]
    public IActionResult Ping() => Ok("pong");
}

public static class OrderMinimalEndpoints
{
    private const string MinimalRoot = "/api/minimal/orders";

    public static void MapOrderEndpoints(IEndpointRouteBuilder app)
    {
        app.MapGet(MinimalRoot + "/{id}", (long id, OrderService service) => service.ReadOrder(id));
        app.MapPost(MinimalRoot, (CreateOrderRequest request, OrderService service) => service.CreateOrder(request));
        app.MapPut(MinimalRoot + "/{id}", (long id) => Results.Ok(id));
        app.MapPatch(MinimalRoot + "/{id}/status", (long id) => Results.Ok(id));
        app.MapDelete(MinimalRoot + "/{id}", (long id) => Results.Ok(id));
        app.MapMethods(MinimalRoot + "/{id}/state", new[] { HttpMethods.Patch, "DELETE" }, (long id) => Results.Ok(id));
    }
}

public class UnusedService
{
    public UnusedDto Read() => new UnusedRepository().Read();
}

public class UnusedRepository
{
    public UnusedDto Read()
    {
        const string sql = "SELECT * FROM ORDER_LINE";
        return new UnusedDto(sql);
    }
}

public record UnusedDto(string Value);

public class OrderService
{
    private readonly OrderRepository _repository;
    public OrderService(OrderRepository repository) => _repository = repository;
    public OrderDto CreateOrder(CreateOrderRequest request)
    {
        return _repository.CreateOrder(request);
    }

    public OrderDto ReadOrder(long orderId)
    {
        return _repository.ReadOrder(orderId);
    }
}

public class OrderRepository
{
    public OrderDto ReadOrder(long orderId)
    {
        const string sql = @"SELECT oh.ORDER_ID, oh.STATUS
FROM ORDER_HEADER oh
JOIN ORDER_LINE ol ON ol.ORDER_ID = oh.ORDER_ID
LEFT JOIN PAYMENT_TRANSACTION pt ON pt.ORDER_ID = oh.ORDER_ID";
        return new OrderDto(orderId, sql);
    }

    public OrderDto CreateOrder(CreateOrderRequest request)
    {
        IOrderCalculator.Calculate(request);
        var duplicate = IOrderCalculator.Calculate(request);
        // AMBIGUOUS_SYMBOL: IOrderCalculator.Calculate has two reachable implementations.
        using var command = new FakeCommand
        {
            CommandText = "PKG_ORDER.CREATE_ORDER",
            CommandType = CommandType.StoredProcedure,
        };
        return new OrderDto(request.CustomerId, command.CommandText + duplicate);
    }

    public void CancelOrder(long orderId)
    {
        using var command = new FakeCommand
        {
            CommandText = "PKG_ORDER.CANCEL_ORDER",
            CommandType = CommandType.StoredProcedure,
        };
        Console.WriteLine(command.CommandText + orderId);
    }


    public void ArchiveLegacy(long orderId)
    { const string missingTable = "SELECT * FROM ORDER_ARCHIVE_TMP WHERE ORDER_ID = :orderId"; Console.WriteLine(missingTable); }

    public void ReadLegacyStatus(long orderId)
    {
        const string missingColumn = "SELECT ORDER_HEADER.LEGACY_STATUS FROM ORDER_HEADER WHERE ORDER_ID = :orderId";
        Console.WriteLine(missingColumn);
    }

    public void SubmitLegacy(long orderId)
    {
        using var command = new FakeCommand
        {
            CommandType = CommandType.StoredProcedure,
            CommandText = "PKG_ORDER.SUBMIT_LEGACY",
        };
        Console.WriteLine(command.CommandText + orderId);
    }

    public void ConfirmShipment(long shipmentId)
    {
        const string upsert = "MERGE INTO PAYMENT_TRANSACTION target USING ORDER_HEADER source ON (target.ORDER_ID = source.ORDER_ID) WHEN MATCHED THEN UPDATE SET target.STATUS = 'CAPTURED'";
        Console.WriteLine(upsert + shipmentId);
    }

    public void FindDynamic(string tableName)
    {
        var sql = $"SELECT * FROM ORDER_APP.{tableName}";
        Console.WriteLine(sql);
    }
}

public static class IOrderCalculator
{
    public static decimal Calculate(CreateOrderRequest request) => request.CustomerId;
}

public sealed class FakeCommand : IDisposable
{
    public string CommandText { get; init; } = "";
    public CommandType CommandType { get; init; }
    public void Dispose() { }
}
