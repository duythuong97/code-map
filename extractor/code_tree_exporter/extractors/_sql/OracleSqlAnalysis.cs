namespace CodeTree.Sql;

public sealed record SqlTableReference(string ObjectName, string Operation, string EdgeType, int Start, bool Remote, string DbLink);

/// <summary>
/// Port of <c>package_support/sql_analyzer.analyze_sql</c>. Offsets are code
/// points into the analysed text (Python <c>str</c> indexes).
/// </summary>
public sealed record OracleSqlAnalysis(
    IReadOnlyList<SqlTableReference> Tables,
    IReadOnlyList<ParsedCallReference> Calls,
    IReadOnlyList<ParsedSequenceReference> Sequences,
    IReadOnlyList<int> DynamicOffsets,
    IReadOnlyList<int> ParseErrorOffsets,
    bool Recognized,
    string Classification)
{
    public static OracleSqlAnalysis Analyze(string text) => From(new OracleSqlParse(text));

    public static OracleSqlAnalysis From(OracleSqlParse parse)
    {
        var tables = parse.TableReferences()
            .Select(reference => new SqlTableReference(
                reference.ObjectName,
                reference.Operation,
                reference.Relation switch { "READS" => "READS_FROM", "WRITES" => "WRITES_TO", var other => other },
                reference.Start,
                reference.DbLink.Length > 0,
                reference.DbLink))
            .ToList();
        var errorOffsets = parse.SyntaxErrors
            .Select(error => OffsetForLineColumn(parse.Source, error.Line, error.Column))
            .ToHashSet();
        return new OracleSqlAnalysis(
            tables,
            parse.Calls(),
            parse.Sequences(),
            parse.DynamicSqlOffsets(),
            FirstOffsetPerLine(parse.Source, errorOffsets),
            parse.HasExecutableStatement(),
            parse.ScriptClassification());
    }

    /// <summary>Python <c>_offset_for_line_column</c> (splitlines(keepends=True)).</summary>
    static int OffsetForLineColumn(SourceText source, int line, int column)
    {
        if (line <= 0) return 0;
        var lineStarts = PythonLineStarts(source);
        var start = line - 1 < lineStarts.Count ? lineStarts[line - 1] : source.Length;
        return Math.Min(start + Math.Max(column, 0), source.Length);
    }

    static List<int> FirstOffsetPerLine(SourceText source, HashSet<int> offsets)
    {
        var first = new Dictionary<int, int>();
        var order = new List<int>();
        foreach (var offset in offsets.OrderBy(value => value))
        {
            var line = source.LineForOffset(offset) - 1;
            if (first.TryAdd(line, offset)) order.Add(line);
        }
        return order.Select(line => first[line]).ToList();
    }

    /// <summary>Start offsets (code points) of the lines Python's splitlines yields.</summary>
    static List<int> PythonLineStarts(SourceText source)
    {
        var starts = new List<int>();
        var text = source.Text;
        var codePoint = 0;
        var lineStart = 0;
        var any = false;
        for (var index = 0; index < text.Length; index++, codePoint++)
        {
            any = true;
            var ch = text[index];
            if (char.IsHighSurrogate(ch) && index + 1 < text.Length && char.IsLowSurrogate(text[index + 1]))
            {
                index++;
                continue;
            }
            var isBreak = ch is '\n' or '\r' or '\v' or '\f' or '\x1c' or '\x1d' or '\x1e' or '\x85' or '\u2028' or '\u2029';
            if (!isBreak) continue;
            if (ch == '\r' && index + 1 < text.Length && text[index + 1] == '\n')
            {
                index++;
                codePoint++;
            }
            starts.Add(lineStart);
            lineStart = codePoint + 1;
            any = false;
        }
        if (any) starts.Add(lineStart);
        return starts;
    }
}
