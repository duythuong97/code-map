using System.Text.RegularExpressions;
using Antlr4.Runtime;
using Antlr4.Runtime.Tree;
using CodeTree.Sql.Grammar;
using P = CodeTree.Sql.Grammar.PlSqlParser;

namespace CodeTree.Sql;

public sealed record SyntaxError(int Line, int Column, string Message);
public sealed record ParsedSqlReference(string ObjectName, string Operation, string Relation, int Start, string DbLink);
public sealed record ParsedCallReference(string ObjectName, int Start);
public sealed record ParsedSequenceReference(string ObjectName, string Operation, int Start);
public sealed record ParsedRoutineParameter(string Name, string Direction, string DataType, string Raw);
public sealed record ParsedRoutineDeclaration(string Kind, string Name, string? ParameterBlock, int Start, int End, string Signature);
public sealed record ParsedTriggerDeclaration(string Name, string TableName, int Start, int End);
public sealed record ParsedSynonymDeclaration(string Name, string TargetName, int Start);
public sealed record ParsedViewDeclaration(string Kind, string Name, int Start, int BodyStart, int End);

/// <summary>
/// One parse of Oracle SQL / PL-SQL text with the grammars-v4 grammar. A line
/// by line port of <c>package_support/oracle_parser.py</c>: every query keeps
/// the Python semantics (code point offsets, ordering, de-duplication) so the
/// Python and .NET backends produce identical graphs.
/// </summary>
public sealed class OracleSqlParse
{
    static readonly Dictionary<string, string> OpToEdge = new()
    {
        ["SELECT"] = "READS_FROM",
        ["INSERT"] = "WRITES_TO",
        ["UPDATE"] = "WRITES_TO",
        ["DELETE"] = "WRITES_TO",
        ["MERGE"] = "WRITES_TO",
    };

    static readonly Regex DotSpacing = new(@"\s*\.\s*", RegexOptions.Compiled | RegexOptions.CultureInvariant);

    readonly List<ParserRuleContext> _nodes = new();

    public OracleSqlParse(string text)
    {
        Source = new SourceText(text);
        var errors = new ErrorCollector();
        var lexer = new PlSqlLexer(CharStreams.fromString(text));
        lexer.RemoveErrorListeners();
        lexer.AddErrorListener(errors);
        var parser = new P(new CommonTokenStream(lexer));
        parser.RemoveErrorListeners();
        parser.AddErrorListener(errors);
        Tree = parser.sql_script();
        SyntaxErrors = errors.Errors;
        Collect(Tree, _nodes);
    }

    public SourceText Source { get; }
    public string Text => Source.Text;
    public P.Sql_scriptContext Tree { get; }
    public IReadOnlyList<SyntaxError> SyntaxErrors { get; }

    /// <summary>Rule contexts in pre-order, like <c>OraclePlsqlParser._walk()</c>.</summary>
    public IReadOnlyList<ParserRuleContext> Nodes => _nodes;

    public string? PackageName()
    {
        foreach (var node in _nodes)
        {
            var names = node switch
            {
                P.Create_packageContext package => package.package_name(),
                P.Create_package_bodyContext body => body.package_name(),
                _ => null,
            };
            if (names is { Length: > 0 }) return SourceText.Upper(Identifier(names[0]));
        }
        return null;
    }

    public List<ParsedRoutineDeclaration> Routines()
    {
        var routines = new List<ParsedRoutineDeclaration>();
        foreach (var node in _nodes)
        {
            (string Kind, ParserRuleContext? Name)? spec = node switch
            {
                P.Create_procedure_bodyContext x => ("PROCEDURE", x.procedure_name()),
                P.Create_function_bodyContext x => ("FUNCTION", x.function_name()),
                P.Procedure_bodyContext x => ("PROCEDURE", x.identifier()),
                P.Function_bodyContext x => ("FUNCTION", x.identifier()),
                P.Procedure_specContext x => ("PROCEDURE", x.identifier()),
                P.Function_specContext x => ("FUNCTION", x.identifier()),
                _ => null,
            };
            if (spec is null) continue;
            var name = SourceText.Upper(LastSegment(Identifier(spec.Value.Name)));
            var start = Start(node);
            var end = Stop(node) + 1;
            routines.Add(new ParsedRoutineDeclaration(
                spec.Value.Kind, name, ParameterBlock(node), start, end, RoutineSignature(start, end)));
        }
        return routines.OrderBy(item => item.Start).ThenBy(item => item.End).ToList();
    }

    public List<ParsedTriggerDeclaration> Triggers()
    {
        var triggers = new List<ParsedTriggerDeclaration>();
        foreach (var node in _nodes)
        {
            if (node is not P.Create_triggerContext trigger) continue;
            var table = Descendants(trigger).OfType<P.Tableview_nameContext>().Select(SourceOf).FirstOrDefault() ?? string.Empty;
            if (table.Length == 0) continue;
            triggers.Add(new ParsedTriggerDeclaration(
                SourceText.Upper(LastSegment(Identifier(trigger.trigger_name()))),
                NormalizeName(table),
                Start(trigger),
                Stop(trigger) + 1));
        }
        return triggers;
    }

    public List<ParsedSynonymDeclaration> Synonyms()
    {
        var synonyms = new List<ParsedSynonymDeclaration>();
        foreach (var node in _nodes)
        {
            if (node is not P.Create_synonymContext synonym) continue;
            var nameNode = synonym.synonym_name();
            var objectNode = synonym.schema_object_name();
            if (nameNode is null || objectNode is null) continue;
            var parts = synonym.schema_name().Select(SourceOf).ToList();
            parts.Add(SourceOf(objectNode));
            var target = string.Join(".", parts.Where(part => part.Length > 0));
            var link = synonym.link_name();
            if (link is not null) target += "@" + SourceOf(link);
            synonyms.Add(new ParsedSynonymDeclaration(Identifier(nameNode), NormalizeName(target), Start(nameNode)));
        }
        return synonyms;
    }

    public List<ParsedViewDeclaration> Views()
    {
        var views = new List<ParsedViewDeclaration>();
        foreach (var node in _nodes)
        {
            string kind;
            List<string> nameParts;
            ParserRuleContext? body;
            if (node is P.Create_viewContext view)
            {
                nameParts = new List<string>();
                if (view.schema_name() is { } schema) nameParts.Add(SourceOf(schema));
                nameParts.Add(SourceOf(view.v));
                body = view.select_only_statement();
                kind = "VIEW";
            }
            else if (node is P.Create_materialized_viewContext materialized)
            {
                nameParts = new List<string> { SourceOf(materialized.tableview_name()) };
                body = materialized.select_only_statement();
                kind = "MATERIALIZED_VIEW";
            }
            else
            {
                continue;
            }
            views.Add(new ParsedViewDeclaration(
                kind, NormalizeName(string.Join(".", nameParts)), Start(node), body is null ? Start(node) : Start(body), Stop(node) + 1));
        }
        return views.OrderBy(item => item.Start).ToList();
    }

    public string ScriptClassification()
    {
        var hasPlsql = false;
        var hasDml = false;
        foreach (var node in _nodes)
        {
            hasPlsql = hasPlsql || node is P.Anonymous_blockContext
                or P.Create_procedure_bodyContext
                or P.Create_function_bodyContext
                or P.Create_packageContext
                or P.Create_package_bodyContext
                or P.Create_triggerContext
                or P.Create_viewContext
                or P.Create_materialized_viewContext
                or P.Create_synonymContext;
            hasDml = hasDml || node is P.Data_manipulation_language_statementsContext;
        }
        if (hasPlsql && hasDml) return "MIXED_SCRIPT";
        if (hasPlsql) return "PLSQL_DEFINITION";
        if (hasDml) return "DML_SCRIPT";
        return "UNKNOWN_SQL";
    }

    /// <summary>Table references classified entirely by grammar ancestry.</summary>
    public List<ParsedSqlReference> TableReferences()
    {
        var cteNames = _nodes.OfType<P.Query_nameContext>()
            .Select(node => SourceText.Upper(NormalizeName(SourceOf(node))))
            .ToHashSet(StringComparer.Ordinal);
        var references = new List<ParsedSqlReference>();
        var seen = new HashSet<(string, string)>();
        foreach (var node in _nodes)
        {
            if (node is not P.Tableview_nameContext table) continue;
            var parts = new List<string> { Identifier(table.identifier()) };
            if (table.id_expression() is { } expression) parts.Add(Identifier(expression));
            var name = string.Join(".", parts.Where(part => part.Length > 0));
            var dbLink = table.link_name() is { } link ? SourceText.Upper(Identifier(link)) : string.Empty;
            if (name.Length == 0 || cteNames.Contains(SourceText.Upper(name))) continue;
            var (operation, relation) = ("SELECT", OpToEdge["SELECT"]);
            for (var ancestor = table.Parent as ParserRuleContext; ancestor is not null; ancestor = ancestor.Parent as ParserRuleContext)
            {
                if (ancestor is P.Select_statementContext) break;
                if (ancestor is P.Update_statementContext) { (operation, relation) = ("UPDATE", OpToEdge["UPDATE"]); break; }
                if (ancestor is P.Delete_statementContext) { (operation, relation) = ("DELETE", OpToEdge["DELETE"]); break; }
                if (ancestor is P.Insert_into_clauseContext) { (operation, relation) = ("INSERT", OpToEdge["INSERT"]); break; }
                if (ancestor is P.Selected_tableviewContext && ancestor.Parent is P.Merge_statementContext merge)
                {
                    var selected = merge.selected_tableview();
                    if (selected.Length > 0 && ReferenceEquals(ancestor, selected[0]))
                        (operation, relation) = ("MERGE", OpToEdge["MERGE"]);
                    break;
                }
            }
            if (seen.Add((SourceText.Upper(name), relation)))
                references.Add(new ParsedSqlReference(name, operation, relation, Start(table), dbLink));
        }
        return references
            .OrderBy(item => item.Start)
            .ThenBy(item => item.Relation, StringComparer.Ordinal)
            .ThenBy(item => item.ObjectName, StringComparer.Ordinal)
            .ToList();
    }

    /// <summary>NEXTVAL/CURRVAL references from parsed qualified expressions.</summary>
    public List<ParsedSequenceReference> Sequences()
    {
        var references = new List<ParsedSequenceReference>();
        var seen = new HashSet<(string, int)>();
        foreach (var node in _nodes)
        {
            if (node is not P.General_elementContext element) continue;
            var parts = SourceOf(element).Split('.').Select(NormalizeName).ToList();
            if (parts.Count < 2) continue;
            var operation = SourceText.Upper(parts[^1]);
            if (operation is not ("NEXTVAL" or "CURRVAL")) continue;
            var name = string.Join(".", parts.Take(parts.Count - 1));
            if (name.Length > 0 && seen.Add((SourceText.Upper(name), Start(element))))
                references.Add(new ParsedSequenceReference(name, operation, Start(element)));
        }
        return references.OrderBy(item => item.Start).ThenBy(item => item.ObjectName, StringComparer.Ordinal).ToList();
    }

    public List<ParsedRoutineParameter> RoutineParameters(int routineStart, int routineEnd)
    {
        var result = new List<ParsedRoutineParameter>();
        foreach (var node in _nodes)
        {
            if (node is not P.ParameterContext parameter) continue;
            if (Start(parameter) < routineStart || Stop(parameter) >= routineEnd) continue;
            var owner = parameter.Parent as ParserRuleContext;
            while (owner is not null && owner is not (P.Create_procedure_bodyContext or P.Create_function_bodyContext
                       or P.Procedure_bodyContext or P.Function_bodyContext or P.Procedure_specContext or P.Function_specContext))
                owner = owner.Parent as ParserRuleContext;
            if (owner is null || Start(owner) != routineStart) continue;
            var direction = parameter.INOUT().Length > 0 ? "IN OUT" : parameter.OUT().Length > 0 ? "OUT" : "IN";
            result.Add(new ParsedRoutineParameter(
                parameter.parameter_name() is { } name ? SourceOf(name) : string.Empty,
                direction,
                parameter.type_spec() is { } type ? SourceOf(type) : string.Empty,
                SourceOf(parameter)));
        }
        return result;
    }

    public string RoutineSignature(int routineStart, int routineEnd)
    {
        var types = RoutineParameters(routineStart, routineEnd)
            .Where(parameter => parameter.DataType.Length > 0)
            .Select(parameter => SourceText.Upper(parameter.DataType))
            .ToList();
        return types.Count > 0 ? string.Join("_", types) : "void";
    }

    public List<ParsedCallReference> Calls()
    {
        var calls = new List<ParsedCallReference>();
        var seen = new HashSet<(int, string)>();
        foreach (var node in _nodes)
        {
            var raw = string.Empty;
            var start = Start(node);
            if (node is P.Call_statementContext call)
            {
                var names = call.routine_name();
                if (names.Length > 0)
                {
                    raw = string.Join(".", names.Select(Identifier));
                    start = Start(names[0]);
                }
            }
            else if (node is P.General_elementContext element)
            {
                var argument = Descendants(element).OfType<P.Function_argumentContext>().FirstOrDefault();
                if (argument is not null) raw = Source.Slice(Start(element), Start(argument));
            }
            if (raw.Length == 0) continue;
            var name = NormalizeName(raw);
            if (name.Length > 0 && seen.Add((start, SourceText.Upper(name))))
                calls.Add(new ParsedCallReference(name, start));
        }
        return calls.OrderBy(item => item.Start).ThenBy(item => item.ObjectName, StringComparer.Ordinal).ToList();
    }

    public List<int> DynamicSqlOffsets()
        => _nodes.OfType<P.Execute_immediateContext>().Select(Start).ToList();

    public bool HasExecutableStatement()
    {
        foreach (var node in _nodes)
        {
            if (node is P.Data_manipulation_language_statementsContext
                or P.Anonymous_blockContext
                or P.Create_procedure_bodyContext
                or P.Create_function_bodyContext
                or P.Create_packageContext
                or P.Create_package_bodyContext
                or P.Create_triggerContext
                or P.Create_viewContext
                or P.Create_materialized_viewContext
                or P.Execute_immediateContext)
                return true;
            if (node is P.Call_statementContext call && call.CALL() is not null) return true;
        }
        return false;
    }

    public static IEnumerable<ParserRuleContext> Descendants(ParserRuleContext root)
    {
        var result = new List<ParserRuleContext>();
        Collect(root, result);
        return result;
    }

    public string SourceOf(IParseTree? node)
    {
        if (node is null) return string.Empty;
        if (node is ITerminalNode terminal) return Source.Slice(terminal.Symbol.StartIndex, terminal.Symbol.StopIndex + 1);
        var context = (ParserRuleContext)node;
        return Source.Slice(Start(context), Stop(context) + 1);
    }

    public string Identifier(IParseTree? node) => NormalizeName(SourceOf(node));

    public static string NormalizeName(string raw)
        => DotSpacing.Replace(SourceText.Strip(raw), ".").Replace("\"", string.Empty);

    public static int Start(ParserRuleContext node) => node.Start?.StartIndex ?? 0;

    /// <summary><c>node.stop.stop</c>; an empty rule ends just before it starts.</summary>
    public static int Stop(ParserRuleContext node) => node.Stop?.StopIndex ?? Start(node) - 1;

    string? ParameterBlock(ParserRuleContext node)
    {
        var (left, right) = node switch
        {
            P.Create_procedure_bodyContext x => (x.LEFT_PAREN(), x.RIGHT_PAREN()),
            P.Create_function_bodyContext x => (x.LEFT_PAREN(), x.RIGHT_PAREN()),
            P.Procedure_bodyContext x => (x.LEFT_PAREN(), x.RIGHT_PAREN()),
            P.Function_bodyContext x => (x.LEFT_PAREN(), x.RIGHT_PAREN()),
            P.Procedure_specContext x => (x.LEFT_PAREN(), x.RIGHT_PAREN()),
            P.Function_specContext x => (x.LEFT_PAREN(), x.RIGHT_PAREN()),
            _ => ((ITerminalNode?)null, (ITerminalNode?)null),
        };
        if (left is null || right is null) return null;
        return Source.Slice(left.Symbol.StartIndex, right.Symbol.StopIndex + 1);
    }

    static string LastSegment(string value)
    {
        var index = value.LastIndexOf('.');
        return index < 0 ? value : value[(index + 1)..];
    }

    static void Collect(ParserRuleContext root, List<ParserRuleContext> into)
    {
        // Iterative pre-order: deeply nested PL/SQL would overflow a recursive walk.
        var stack = new Stack<ParserRuleContext>();
        stack.Push(root);
        while (stack.Count > 0)
        {
            var node = stack.Pop();
            into.Add(node);
            if (node.children is null) continue;
            for (var index = node.children.Count - 1; index >= 0; index--)
                if (node.children[index] is ParserRuleContext child)
                    stack.Push(child);
        }
    }

    sealed class ErrorCollector : IAntlrErrorListener<int>, IAntlrErrorListener<IToken>
    {
        public List<SyntaxError> Errors { get; } = new();

        public void SyntaxError(TextWriter output, IRecognizer recognizer, int offendingSymbol, int line, int charPositionInLine, string msg, RecognitionException e)
            => Errors.Add(new SyntaxError(line, charPositionInLine, msg));

        public void SyntaxError(TextWriter output, IRecognizer recognizer, IToken offendingSymbol, int line, int charPositionInLine, string msg, RecognitionException e)
            => Errors.Add(new SyntaxError(line, charPositionInLine, msg));
    }
}
