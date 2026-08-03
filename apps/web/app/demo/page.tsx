import type { Metadata } from "next";
import { DemoWorkspace } from "@/app/demo/demo-workspace";

export const metadata: Metadata = {
  title: "TAKEGRAPH Demo — ORBIT live build",
  description: "Scoped guest demo of the ORBIT selective rebuild path.",
};

export default function DemoPage() {
  return <DemoWorkspace />;
}
