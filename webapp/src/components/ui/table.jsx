import React from "react";
import { cn } from "../../lib/utils";

export function Table({ className, ...props }) {
  return (
    <div className="relative w-full overflow-auto rounded-lg border border-slate-800 bg-slate-950/45">
      <table className={cn("ui-table w-full caption-bottom text-sm", className)} {...props} />
    </div>
  );
}

export function TableHeader({ className, ...props }) {
  return <thead className={cn("[&_tr]:border-b [&_tr]:border-slate-800", className)} {...props} />;
}

export function TableBody({ className, ...props }) {
  return <tbody className={cn("[&_tr:last-child]:border-0", className)} {...props} />;
}

export function TableRow({ className, ...props }) {
  return <tr className={cn("border-b border-slate-800 transition-colors hover:bg-slate-900/70", className)} {...props} />;
}

export function TableHead({ className, ...props }) {
  return <th className={cn("h-8 px-2 text-left align-middle text-xs font-semibold uppercase tracking-wide text-slate-400", className)} {...props} />;
}

export function TableCell({ className, ...props }) {
  return <td className={cn("px-2 py-1.5 align-top text-slate-200", className)} {...props} />;
}
