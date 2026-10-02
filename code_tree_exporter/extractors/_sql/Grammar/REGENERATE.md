# C# PL/SQL grammar (maintainers only)

`PlSqlLexer.cs` and `PlSqlParser.cs` are generated from the same grammars-v4
revision as the Python parser (`package_support/antlr_plsql_generated/PROVENANCE.md`,
commit `e756f2a2ee5565a9300666f100ba6acd874664f7`, `sql/plsql`).

1. Download `antlr-4.13.2-complete.jar` outside the repository.
2. In grammars-v4 `sql/plsql`:

   `java -jar antlr-4.13.2-complete.jar -Dlanguage=CSharp -package CodeTree.Sql.Grammar -no-listener -no-visitor PlSqlLexer.g4 PlSqlParser.g4`

3. Copy `PlSqlLexer.cs` and `PlSqlParser.cs` here. `PlSqlLexerBase.cs` and
   `PlSqlParserBase.cs` are grammars-v4 `sql/plsql/CSharp` with
   `namespace CodeTree.Sql.Grammar;` added.
4. Re-run the parity check against the Python parser
   (`tests/test_dotnet_sql_parity.py`).
