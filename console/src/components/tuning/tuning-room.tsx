"use client";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

import { ConfigKnobs } from "./config-knobs";
import { PromptsEditor } from "./prompts-editor";

export function TuningRoom() {
  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4 p-4 sm:p-6">
      <Tabs defaultValue="config">
        <TabsList>
          <TabsTrigger value="config">Config</TabsTrigger>
          <TabsTrigger value="prompts">Prompts</TabsTrigger>
        </TabsList>
        <TabsContent value="config" className="mt-4">
          <p className="mb-3 text-sm text-muted-foreground">
            Veracity-core knobs. Propose stages a value; Promote enacts it (sign-off-gated, D28).
          </p>
          <ConfigKnobs />
        </TabsContent>
        <TabsContent value="prompts" className="mt-4">
          <p className="mb-3 text-sm text-muted-foreground">
            The live agent prompts. Editing publishes a new active version (sign-off-gated, D29).
          </p>
          <PromptsEditor />
        </TabsContent>
      </Tabs>
    </div>
  );
}
