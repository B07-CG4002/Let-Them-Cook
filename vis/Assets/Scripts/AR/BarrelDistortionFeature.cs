using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.Rendering.RenderGraphModule;
using UnityEngine.Rendering.Universal;


public class BarrelDistortionFeature : ScriptableRendererFeature
{
    [System.Serializable]
    public class Settings
    {
        [Tooltip("Start near 0 and increase until straight lines viewed through the lens look straight, not curved inward.")]
        [Range(0f, 1f)] public float k1 = 0.34f;

        [Tooltip("Fine-tune correction toward the edges after k1 looks right in the center.")]
        [Range(0f, 1f)] public float k2 = 0.55f;

        public RenderPassEvent renderPassEvent = RenderPassEvent.AfterRenderingPostProcessing;
    }

    public Settings settings = new Settings();
    private BarrelDistortionPass pass;
    private Material material;

    public override void Create()
    {
        var shader = Shader.Find("Hidden/BarrelDistortion");
        if (shader != null)
            material = CoreUtils.CreateEngineMaterial(shader);

        pass = new BarrelDistortionPass(material);
    }

    public override void AddRenderPasses(ScriptableRenderer renderer, ref RenderingData renderingData)
    {
        if (material == null)
            return;

        pass.renderPassEvent = settings.renderPassEvent;
        pass.SetSettings(settings);
        renderer.EnqueuePass(pass);
    }

    protected override void Dispose(bool disposing)
    {
        CoreUtils.Destroy(material);
    }

    private class BarrelDistortionPass : ScriptableRenderPass
    {
        private readonly Material material;
        private Settings settings;

        private class PassData
        {
            public TextureHandle source;
            public Material material;
            public float k1;
            public float k2;
        }

        public BarrelDistortionPass(Material material)
        {
            this.material = material;
        }

        public void SetSettings(Settings settings)
        {
            this.settings = settings;
        }

        public override void RecordRenderGraph(RenderGraph renderGraph, ContextContainer frameData)
        {
            var resourceData = frameData.Get<UniversalResourceData>();
            var source = resourceData.activeColorTexture;

            var descriptor = renderGraph.GetTextureDesc(source);
            descriptor.name = "_BarrelDistortionTemp";
            descriptor.clearBuffer = false;
            var destination = renderGraph.CreateTexture(descriptor);

            using (var builder = renderGraph.AddRasterRenderPass<PassData>("Barrel Distortion", out var passData))
            {
                passData.source = source;
                passData.material = material;
                passData.k1 = settings.k1;
                passData.k2 = settings.k2;

                builder.UseTexture(source, AccessFlags.Read);
                builder.SetRenderAttachment(destination, 0, AccessFlags.Write);

                builder.SetRenderFunc((PassData data, RasterGraphContext context) =>
                {
                    data.material.SetFloat("_K1", data.k1);
                    data.material.SetFloat("_K2", data.k2);
                    Blitter.BlitTexture(context.cmd, data.source, new Vector4(1, 1, 0, 0), data.material, 0);
                });
            }

            resourceData.cameraColor = destination;
        }
    }
}
