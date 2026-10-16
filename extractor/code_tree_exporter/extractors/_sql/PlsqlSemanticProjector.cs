using System.Collections;
using System.Collections.Concurrent;
using System.Reflection;
using Antlr4.Runtime;
using Antlr4.Runtime.Tree;
using P = CodeTree.Sql.Grammar.PlSqlParser;

namespace CodeTree.Sql;

/// <summary>
/// Port of <c>semantic_tree_v3._PlsqlProjector</c>: projects ANTLR syntax into
/// nested behaviour facts (full detail). Linking a call or data effect to a
/// graph node needs the Python package builder, so those facts carry a
/// <c>_ref</c> marker ({name, edge_types}) that the caller resolves into
/// <c>resolution</c>/<c>ref_node_id</c>; summarising also stays with the caller.
/// </summary>
public sealed class PlsqlSemanticProjector
{
    static readonly ConcurrentDictionary<(Type, string), MethodInfo?> Accessors = new();

    readonly OracleSqlParse _parse;
    readonly SourceText _text;
    readonly string _sourcePath;
    readonly int _baseLine;
    readonly List<ParsedCallReference> _calls;

    PlsqlSemanticProjector(string text, string sourcePath, int baseLine)
    {
        _parse = new OracleSqlParse(StandaloneRoutineText(text));
        _text = _parse.Source;
        _sourcePath = sourcePath;
        _baseLine = baseLine;
        _calls = _parse.Calls();
    }

    public static List<Dictionary<string, object?>> Steps(string text, string sourcePath, int baseLine)
        => new PlsqlSemanticProjector(text, sourcePath, baseLine).Steps();

    /// <summary>Port of <c>semantic_tree_v3.sql_facts</c>, without resolution.</summary>
    public static List<Dictionary<string, object?>> SqlFacts(string text, string sourcePath, int baseLine)
    {
        var source = new SourceText(text);
        var facts = new List<Dictionary<string, object?>>();
        foreach (var reference in OracleSqlAnalysis.Analyze(text).Tables)
        {
            var fact = Fact("data_effect", $"{reference.Operation} {SourceText.Upper(reference.ObjectName)}", reference.Start, source, sourcePath, baseLine);
            fact["action"] = reference.Operation;
            fact["_ref"] = Ref(reference.ObjectName, reference.EdgeType);
            facts.Add(fact);
        }
        return facts
            .OrderBy(fact => ((Dictionary<string, object?>)fact["source"]!)["line"])
            .ThenBy(fact => (string)fact["type"]!, StringComparer.Ordinal)
            .ThenBy(fact => (string)fact["label"]!, StringComparer.Ordinal)
            .ToList();
    }

    List<Dictionary<string, object?>> Steps()
    {
        var bodies = _parse.Nodes.OfType<P.BodyContext>().ToList();
        if (bodies.Count == 0)
        {
            var facts = SqlFacts(_text.Text, _sourcePath, _baseLine);
            if (facts.Count > 0) return facts;
            var compact = SourceText.Compact(_text.Text);
            var statement = MakeFact("statement", 0, compact);
            statement["expression"] = compact;
            statement["resolution"] = "partial";
            return new List<Dictionary<string, object?>> { statement };
        }
        // Python max(): the first body with the largest span.
        var body = bodies[0];
        foreach (var candidate in bodies)
            if (Span(candidate) > Span(body)) body = candidate;
        return Body(body);
    }

    static int Span(ParserRuleContext node) => OracleSqlParse.Stop(node) - OracleSqlParse.Start(node);

    List<Dictionary<string, object?>> Body(P.BodyContext body)
    {
        var steps = Declarations(body.Parent as ParserRuleContext);
        steps.AddRange(Sequence(body.seq_of_statements()));
        var catches = body.exception_handler().Select(Exception).ToList();
        if (catches.Count == 0) return steps;
        var fact = MakeFact("try", OracleSqlParse.Start(body), "BEGIN / EXCEPTION");
        fact["steps"] = steps;
        fact["catches"] = catches;
        fact["finally_steps"] = new List<Dictionary<string, object?>>();
        return new List<Dictionary<string, object?>> { fact };
    }

    List<Dictionary<string, object?>> Declarations(ParserRuleContext? owner)
    {
        var facts = new List<Dictionary<string, object?>>();
        if (owner is null) return facts;
        IEnumerable<P.Declare_specContext> specs;
        var sequence = Invoke(owner, "seq_of_declare_specs") as P.Seq_of_declare_specsContext;
        if (sequence is not null)
            specs = sequence.declare_spec();
        else if (Invoke(owner, "declare_spec") is IEnumerable values)
            specs = values.OfType<P.Declare_specContext>();
        else
            specs = Array.Empty<P.Declare_specContext>();
        foreach (var spec in specs)
        {
            var variable = spec.variable_declaration();
            var @default = variable?.default_value_part();
            if (variable is null || @default is null) continue;
            var target = Text(variable.identifier());
            var expression = Text(@default.expression());
            var calls = Calls(OracleSqlParse.Start(@default), OracleSqlParse.Stop(@default) + 1);
            var fact = MakeFact("assignment", OracleSqlParse.Start(@default), $"Initialize {target}");
            fact["target"] = target;
            fact["expression"] = expression;
            fact["action"] = "INITIALIZE";
            if (calls.Count > 0) fact["effects"] = calls;
            facts.Add(fact);
        }
        return facts;
    }

    List<Dictionary<string, object?>> Sequence(P.Seq_of_statementsContext? sequence)
        => sequence is null ? new() : sequence.statement().Select(Statement).ToList();

    Dictionary<string, object?> Statement(P.StatementContext statement)
    {
        if (statement.if_statement() is { } ifStatement) return If(ifStatement);
        if (statement.loop_statement() is { } loop)
        {
            var iterator = loop.cursor_loop_param() is { } param ? Text(param) : string.Empty;
            var condition = loop.condition() is { } loopCondition ? Text(loopCondition) : string.Empty;
            var label = SourceText.Compact(iterator.Length > 0 ? iterator : condition.Length > 0 ? condition : "LOOP");
            var fact = MakeFact("loop", OracleSqlParse.Start(loop), label);
            fact["condition"] = condition;
            fact["iterator"] = iterator;
            fact["steps"] = Sequence(loop.seq_of_statements());
            return fact;
        }
        if (statement.case_statement() is { } caseStatement) return Case(caseStatement);
        if (statement.assignment_statement() is { } assignment)
        {
            var target = Text((IParseTree?)assignment.general_element() ?? assignment.bind_variable());
            var expression = Text(assignment.expression());
            var calls = Calls(OracleSqlParse.Start(assignment), OracleSqlParse.Stop(assignment) + 1);
            var fact = MakeFact("assignment", OracleSqlParse.Start(assignment), $"Set {target}");
            fact["target"] = target;
            fact["expression"] = expression;
            fact["action"] = ":=";
            if (calls.Count > 0) fact["effects"] = calls;
            return fact;
        }
        if (statement.return_statement() is { } returnStatement)
        {
            var expression = returnStatement.expression() is { } value ? Text(value) : string.Empty;
            var fact = MakeFact("output", OracleSqlParse.Start(returnStatement), $"Return {SourceText.Compact(expression)}".TrimEnd());
            fact["expression"] = expression;
            fact["action"] = "RETURN";
            return fact;
        }
        if (statement.raise_statement() is { } raise)
        {
            var expression = raise.exception_name() is { } name ? Text(name) : string.Empty;
            var fact = MakeFact("raise", OracleSqlParse.Start(raise), $"Raise {expression}".TrimEnd());
            fact["expression"] = expression;
            fact["action"] = "RAISE";
            return fact;
        }
        if (statement.continue_statement() is { } continueStatement)
        {
            var fact = MakeFact("continue", OracleSqlParse.Start(continueStatement), "Continue");
            fact["condition"] = continueStatement.condition() is { } condition ? Text(condition) : string.Empty;
            return fact;
        }
        if (statement.exit_statement() is { } exit)
        {
            var fact = MakeFact("exit", OracleSqlParse.Start(exit), "Exit loop");
            fact["condition"] = exit.condition() is { } condition ? Text(condition) : string.Empty;
            return fact;
        }
        if (statement.sql_statement() is { } sql) return Sql(sql);
        if (statement.call_statement() is not null)
        {
            var calls = Calls(OracleSqlParse.Start(statement), OracleSqlParse.Stop(statement) + 1);
            if (calls.Count == 1) return calls[0];
            var compact = SourceText.Compact(Text(statement));
            var fact = MakeFact("statement", OracleSqlParse.Start(statement), compact);
            fact["expression"] = compact;
            fact["effects"] = calls;
            fact["resolution"] = "partial";
            return fact;
        }
        var nested = statement.body() ?? statement.block()?.body();
        if (nested is not null)
        {
            var fact = MakeFact("block", OracleSqlParse.Start(statement), "Nested block");
            fact["steps"] = Body(nested);
            return fact;
        }
        var text = SourceText.Compact(Text(statement));
        var other = MakeFact("statement", OracleSqlParse.Start(statement), text);
        other["expression"] = text;
        other["resolution"] = "partial";
        return other;
    }

    Dictionary<string, object?> If(P.If_statementContext node)
    {
        var elseSteps = node.else_part() is { } elsePart ? Sequence(elsePart.seq_of_statements()) : new();
        foreach (var part in node.elsif_part().Reverse())
        {
            var partCondition = Text(part.condition());
            var branch = MakeFact("branch", OracleSqlParse.Start(part), $"ELSIF {SourceText.Compact(partCondition)}");
            branch["condition"] = partCondition;
            branch["steps"] = Sequence(part.seq_of_statements());
            branch["else_steps"] = elseSteps;
            elseSteps = new List<Dictionary<string, object?>> { branch };
        }
        var condition = Text(node.condition());
        var fact = MakeFact("branch", OracleSqlParse.Start(node), $"IF {SourceText.Compact(condition)}");
        fact["condition"] = condition;
        fact["steps"] = Sequence(node.seq_of_statements());
        fact["else_steps"] = elseSteps;
        return fact;
    }

    Dictionary<string, object?> Case(P.Case_statementContext node)
    {
        ParserRuleContext caseNode = (ParserRuleContext?)node.simple_case_statement() ?? node.searched_case_statement();
        var expression = caseNode is P.Simple_case_statementContext simple && simple.expression() is { } value ? Text(value) : string.Empty;
        var (whens, otherwise) = caseNode switch
        {
            P.Simple_case_statementContext simpleCase => (simpleCase.case_when_part_statement(), simpleCase.case_else_part_statement()),
            P.Searched_case_statementContext searched => (searched.case_when_part_statement(), searched.case_else_part_statement()),
            _ => (Array.Empty<P.Case_when_part_statementContext>(), null),
        };
        var cases = new List<Dictionary<string, object?>>();
        foreach (var part in whens)
        {
            var condition = Text(part.expression());
            var fact = MakeFact("case", OracleSqlParse.Start(part), $"WHEN {SourceText.Compact(condition)}");
            fact["condition"] = condition;
            fact["steps"] = Sequence(part.seq_of_statements());
            cases.Add(fact);
        }
        var result = MakeFact("branch", OracleSqlParse.Start(caseNode), $"CASE {SourceText.Compact(expression)}".TrimEnd());
        result["expression"] = expression;
        result["cases"] = cases;
        result["else_steps"] = otherwise is null ? new List<Dictionary<string, object?>>() : Sequence(otherwise.seq_of_statements());
        result["steps"] = new List<Dictionary<string, object?>>();
        return result;
    }

    Dictionary<string, object?> Exception(P.Exception_handlerContext node)
    {
        var condition = string.Join(" OR ", node.exception_name().Select(name => Text(name)));
        var fact = MakeFact("catch", OracleSqlParse.Start(node), condition);
        fact["condition"] = condition;
        fact["steps"] = Sequence(node.seq_of_statements());
        return fact;
    }

    Dictionary<string, object?> Sql(P.Sql_statementContext node)
    {
        var raw = Text(node);
        var start = OracleSqlParse.Start(node);
        var facts = SqlFacts(raw, _sourcePath, _baseLine + _text.LineForOffset(start) - 1);
        facts.AddRange(Calls(start, OracleSqlParse.Stop(node) + 1));
        if (facts.Count == 1) return facts[0];
        var words = SourceText.SplitWhitespace(raw);
        var action = words.Count > 0 ? SourceText.Upper(words[0]) : "SQL";
        var compact = SourceText.Compact(raw);
        var fact = MakeFact("sql", start, compact);
        fact["action"] = action;
        fact["expression"] = compact;
        fact["effects"] = facts;
        return fact;
    }

    List<Dictionary<string, object?>> Calls(int start, int end)
    {
        var facts = new List<Dictionary<string, object?>>();
        foreach (var call in _calls)
        {
            if (!(start <= call.Start && call.Start < end)) continue;
            var fact = MakeFact("call", call.Start, $"Call {call.ObjectName}");
            fact["arguments"] = CallArguments(_text.Slice(call.Start, end));
            fact["_ref"] = Ref(call.ObjectName, "CALLS", "CALLS_API");
            facts.Add(fact);
        }
        return facts;
    }

    Dictionary<string, object?> MakeFact(string kind, int offset, string label)
        => Fact(kind, label, offset, _text, _sourcePath, _baseLine);

    static Dictionary<string, object?> Fact(string kind, string label, int offset, SourceText text, string sourcePath, int baseLine)
        => new()
        {
            ["type"] = kind,
            ["label"] = label,
            ["source"] = new Dictionary<string, object?>
            {
                ["path"] = sourcePath,
                ["line"] = baseLine + text.LineForOffset(offset) - 1,
            },
        };

    static Dictionary<string, object?> Ref(string name, params string[] edgeTypes)
        => new() { ["name"] = name, ["edge_types"] = edgeTypes };

    string Text(IParseTree? node) => node is null ? string.Empty : _parse.SourceOf(node);

    static object? Invoke(ParserRuleContext owner, string name)
    {
        var method = Accessors.GetOrAdd((owner.GetType(), name),
            key => key.Item1.GetMethod(key.Item2, BindingFlags.Public | BindingFlags.Instance, Type.EmptyTypes));
        return method?.Invoke(owner, null);
    }

    /// <summary>Port of <c>_standalone_routine_text</c>.</summary>
    static string StandaloneRoutineText(string text)
    {
        var stripped = text.TrimStart();
        var leading = text.Length - stripped.Length;
        var words = SourceText.SplitWhitespace(stripped);
        var keyword = words.Count > 0 ? SourceText.Upper(words[0]) : string.Empty;
        return keyword is "PROCEDURE" or "FUNCTION" ? text[..leading] + "CREATE OR REPLACE " + text[leading..] : text;
    }

    /// <summary>Port of <c>_call_arguments</c>.</summary>
    static List<string> CallArguments(string raw)
    {
        var left = raw.IndexOf('(');
        if (left < 0) return new();
        var depth = 0;
        var quote = false;
        for (var index = left; index < raw.Length; index++)
        {
            var ch = raw[index];
            if (ch == '\'') quote = !quote;
            else if (!quote && ch == '(') depth++;
            else if (!quote && ch == ')')
            {
                depth--;
                if (depth == 0)
                    return SplitTopLevel(raw.Substring(left + 1, index - left - 1))
                        .Where(value => SourceText.Strip(value).Length > 0)
                        .Select(value => SourceText.Compact(value))
                        .ToList();
            }
        }
        return new();
    }

    static List<string> SplitTopLevel(string value)
    {
        var parts = new List<string>();
        var start = 0;
        var depth = 0;
        var quote = false;
        for (var index = 0; index < value.Length; index++)
        {
            var ch = value[index];
            if (ch == '\'') quote = !quote;
            else if (!quote && ch == '(') depth++;
            else if (!quote && ch == ')') depth = Math.Max(0, depth - 1);
            else if (!quote && ch == ',' && depth == 0)
            {
                parts.Add(value.Substring(start, index - start));
                start = index + 1;
            }
        }
        parts.Add(value[start..]);
        return parts;
    }
}
