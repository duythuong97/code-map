using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using CodeTree.Sql;

// Protocol: one JSON request per stdin line, one JSON response per stdout line.
//   {"id":1,"op":"parse","texts":["..."]}
//       -> {"id":1,"results":[<parse summary>, ...]}
//   {"id":2,"op":"steps","items":[{"text":"...","source_path":"a.sql","base_line":1}]}
//       -> {"id":2,"results":[[<fact>, ...], ...]}
// Items in one request are processed in parallel; results keep request order.
// Offsets are Unicode code points, matching Python str indexes.

var json = new JsonSerializerOptions
{
    PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
    Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
};
var parallel = new ParallelOptions
{
    MaxDegreeOfParallelism = int.TryParse(Environment.GetEnvironmentVariable("CODE_TREE_SQL_THREADS"), out var threads) && threads > 0
        ? threads
        : Environment.ProcessorCount,
};

using var input = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false));
using var output = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = false };
string? line;
while ((line = input.ReadLine()) is not null)
{
    if (line.Length == 0) continue;
    JsonNode? id = null;
    string response;
    try
    {
        var request = JsonNode.Parse(line)!.AsObject();
        id = request["id"]?.DeepClone();
        var op = request["op"]?.GetValue<string>();
        object?[] results = op switch
        {
            "parse" => Run(request["texts"]!.AsArray(), item => Summary(new OracleSqlParse(item!.GetValue<string>()))),
            "steps" => Run(request["items"]!.AsArray(), item => PlsqlSemanticProjector.Steps(
                item!["text"]!.GetValue<string>(),
                item["source_path"]?.GetValue<string>() ?? string.Empty,
                item["base_line"]?.GetValue<int>() ?? 1)),
            _ => throw new InvalidOperationException($"unknown op: {op}"),
        };
        response = JsonSerializer.Serialize(new { id, results }, json);
    }
    catch (Exception exc)
    {
        response = JsonSerializer.Serialize(new { id, error = exc.ToString() }, json);
    }
    output.WriteLine(response);
    output.Flush();
}
return 0;

object?[] Run(JsonArray items, Func<JsonNode?, object?> work)
{
    var results = new object?[items.Count];
    Parallel.For(0, items.Count, parallel, index => results[index] = work(items[index]));
    return results;
}

static object Summary(OracleSqlParse parse) => new
{
    SyntaxErrors = parse.SyntaxErrors.Select(error => new object[] { error.Line, error.Column, error.Message }),
    PackageName = parse.PackageName(),
    Routines = parse.Routines(),
    Triggers = parse.Triggers(),
    Synonyms = parse.Synonyms(),
    Views = parse.Views(),
    Tables = parse.TableReferences(),
    Calls = parse.Calls(),
    Sequences = parse.Sequences(),
    DynamicOffsets = parse.DynamicSqlOffsets(),
    HasExecutableStatement = parse.HasExecutableStatement(),
    ScriptClassification = parse.ScriptClassification(),
};
