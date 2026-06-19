"use client";

import { useState } from "react";

import { ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

/**
 * A sign-off-gated action (D28/D18): the veracity core — gate floor, scoring, prompts — needs
 * explicit confirmation. Click → a confirm dialog that shows what changes → apply. Reused by the
 * config promote + prompt save flows.
 */
export function SignoffButton({
  label,
  title,
  description,
  confirmLabel = "Apply — I'm signing off",
  onConfirm,
  disabled,
  icon,
}: {
  label: string;
  title: string;
  description: React.ReactNode;
  confirmLabel?: string;
  onConfirm: () => void;
  disabled?: boolean;
  icon?: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button size="sm" disabled={disabled} onClick={() => setOpen(true)} className="gap-1.5">
        {icon ?? <ShieldCheck />} {label}
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <ShieldCheck className="size-4 text-amber-500" /> {title}
            </DialogTitle>
            <DialogDescription asChild>
              <div className="space-y-2 text-sm">{description}</div>
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              size="sm"
              onClick={() => {
                setOpen(false);
                onConfirm();
              }}
            >
              {confirmLabel}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
