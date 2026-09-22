import { NavLink } from "react-router-dom";

const linkClasses = ({ isActive }: { isActive: boolean }): string =>
  [
    "rounded px-3 py-1 text-xs transition-colors",
    isActive ? "bg-raised text-ink" : "text-ink-3 hover:text-ink-2",
  ].join(" ");

export function Nav() {
  return (
    <nav aria-label="Views" className="flex items-center gap-1 rounded border border-line p-0.5">
      <NavLink to="/" end className={linkClasses}>
        Dashboard
      </NavLink>
      <NavLink to="/stats" className={linkClasses}>
        Statistics
      </NavLink>
      <NavLink to="/history" className={linkClasses}>
        History
      </NavLink>
    </nav>
  );
}
