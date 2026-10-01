import { Loader2Icon } from "lucide-react";
import { cn } from "@/lib/utils";

// Spin the HTML wrapper, not the svg: Chrome only composites SVG transform
// animations at zoom 1, so on HiDPI screens an svg spin repaints every frame.
export function RunningDot({ className }: { className?: string }) {
  return (
    <span
      aria-hidden
      role="presentation"
      data-testid="running-dot"
      className={cn("inline-flex size-3 shrink-0 animate-spin text-muted-foreground", className)}
    >
      <Loader2Icon className="size-full" />
    </span>
  );
}
