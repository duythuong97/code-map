using System;
using System.Data;
// ORDER_FULFILLMENT jobs:
// ALLOCATE_ORDERS -> CREATE_SHIPMENTS -> RECONCILE_ORDERS
// UNMAPPED_EXPORT PartnerExport.exe intentionally has no mapping.

namespace Demo.OrderFulfillment;

public sealed class Program
{
    private readonly FulfillmentRepository _repository = new();

    public static int Main(string[] args)
    {
        var mode = args.Length > 1 && args[0] == "--mode" ? args[1] : "allocate";
        var program = new Program();
        if (mode == "allocate")
        {
            return program.Allocate();
        }
        if (mode == "ship")
        {
            return program.CreateShipments();
        }
        if (mode == "reconcile")
        {
            return program.Reconcile();
        }
        return 2;
    }

    private int Allocate()
    {
        _repository.AllocateOrder(1001);
        return 0;
    }

    private int CreateShipments()
    {
        _repository.CreateShipment(1001);
        return 0;
    }

    private int Reconcile()
    {
        _repository.ReconcileOrders();
        return 0;
    }
}

public sealed class FulfillmentRepository
{
    public void AllocateOrder(long orderId)
    {
        using var command = new FakeCommand
        {
            CommandText = "PKG_ORDER.ALLOCATE_ORDER",
            CommandType = CommandType.StoredProcedure,
        };
        ExecuteProcedure(command.CommandText, orderId);
    }

    public void CreateShipment(long orderId)
    {
        const string sql = "INSERT INTO SHIPMENT (SHIPMENT_ID, ORDER_ID, STATUS) VALUES (SHIPMENT_SEQ.NEXTVAL, :orderId, 'CREATED')";
        Console.WriteLine(sql);
    }

    public void ReconcileOrders()
    {
        const string sql = "MERGE INTO ORDER_DAILY_SUMMARY dst USING ORDER_HEADER src ON (dst.SUMMARY_DATE = TRUNC(SYSDATE)) WHEN MATCHED THEN UPDATE SET dst.ORDER_COUNT = dst.ORDER_COUNT";
        Console.WriteLine(sql);
    }

    private static void ExecuteProcedure(string procedureName, long orderId)
    {
        Console.WriteLine($"{procedureName}:{orderId}");
    }
}

public sealed class FakeCommand : IDisposable
{
    public string CommandText { get; init; } = "";
    public CommandType CommandType { get; init; }
    public void Dispose() { }
}
