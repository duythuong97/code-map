using System.Text;

namespace CodeTree.Sql;

/// <summary>
/// Source text addressed by Unicode code point, the unit ANTLR's
/// CodePointCharStream and Python's str both use, so offsets match the Python
/// extractors exactly. <see cref="ToUtf16"/> converts for C# string callers.
/// </summary>
public sealed class SourceText
{
    readonly int[]? _utf16ByCodePoint;

    public SourceText(string text)
    {
        Text = text;
        var hasSurrogates = false;
        foreach (var ch in text)
        {
            if (char.IsSurrogate(ch))
            {
                hasSurrogates = true;
                break;
            }
        }
        if (!hasSurrogates)
        {
            Length = text.Length;
            return;
        }
        var map = new List<int>(text.Length + 1);
        for (var index = 0; index < text.Length; index++)
        {
            map.Add(index);
            if (char.IsHighSurrogate(text[index]) && index + 1 < text.Length && char.IsLowSurrogate(text[index + 1]))
                index++;
        }
        Length = map.Count;
        map.Add(text.Length);
        _utf16ByCodePoint = map.ToArray();
    }

    public string Text { get; }

    /// <summary>Length in code points.</summary>
    public int Length { get; }

    public int ToUtf16(int codePointOffset)
    {
        var clamped = Math.Clamp(codePointOffset, 0, Length);
        return _utf16ByCodePoint is null ? clamped : _utf16ByCodePoint[clamped];
    }

    /// <summary>Python <c>text[start:end]</c> with code point offsets.</summary>
    public string Slice(int start, int end)
    {
        start = Math.Clamp(start, 0, Length);
        end = Math.Clamp(end, 0, Length);
        if (end <= start) return string.Empty;
        var from = ToUtf16(start);
        return Text.Substring(from, ToUtf16(end) - from);
    }

    /// <summary>Python <c>text.count("\n", 0, offset) + 1</c>.</summary>
    public int LineForOffset(int offset)
    {
        var end = ToUtf16(Math.Max(0, offset));
        var line = 1;
        for (var index = 0; index < end; index++)
            if (Text[index] == '\n') line++;
        return line;
    }

    /// <summary>Python <c>" ".join(value.split())[:limit]</c>.</summary>
    public static string Compact(string value, int limit = 180)
    {
        var builder = new StringBuilder(value.Length);
        var pendingSpace = false;
        foreach (var ch in value)
        {
            if (IsPythonWhitespace(ch))
            {
                pendingSpace = builder.Length > 0;
                continue;
            }
            if (pendingSpace)
            {
                builder.Append(' ');
                pendingSpace = false;
            }
            builder.Append(ch);
        }
        return TakeCodePoints(builder.ToString(), limit);
    }

    /// <summary>Python <c>str.split()</c> with no separator.</summary>
    public static List<string> SplitWhitespace(string value)
    {
        var parts = new List<string>();
        var builder = new StringBuilder();
        foreach (var ch in value)
        {
            if (IsPythonWhitespace(ch))
            {
                if (builder.Length > 0)
                {
                    parts.Add(builder.ToString());
                    builder.Clear();
                }
                continue;
            }
            builder.Append(ch);
        }
        if (builder.Length > 0) parts.Add(builder.ToString());
        return parts;
    }

    /// <summary>Python <c>str.strip()</c>.</summary>
    public static string Strip(string value)
    {
        var start = 0;
        var end = value.Length;
        while (start < end && IsPythonWhitespace(value[start])) start++;
        while (end > start && IsPythonWhitespace(value[end - 1])) end--;
        return value.Substring(start, end - start);
    }

    /// <summary>Python <c>str.upper()</c> for the identifiers SQL uses.</summary>
    public static string Upper(string value) => value.ToUpperInvariant();

    public static bool IsPythonWhitespace(char ch)
        => ch is ' ' or '\t' or '\n' or '\r' or '\v' or '\f' or '\x1c' or '\x1d' or '\x1e' or '\x1f' or '\x85'
           || (ch > '\x7f' && char.IsWhiteSpace(ch));

    static string TakeCodePoints(string value, int limit)
    {
        var count = 0;
        for (var index = 0; index < value.Length; index++)
        {
            if (count == limit) return value.Substring(0, index);
            if (char.IsHighSurrogate(value[index]) && index + 1 < value.Length && char.IsLowSurrogate(value[index + 1]))
                index++;
            count++;
        }
        return value;
    }
}
