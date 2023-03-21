import taichi as ti
import meshtaichi_patcher as mp


@ti.data_oriented
class LoadModel:
    def __init__(self,
                 filename,
                 ):
        # load_mesh
        model_type = filename.split('.')[-1]
        if model_type == "node":
            self.mesh_rawdata = mp.load_mesh_rawdata(filename)
            self.mesh = mp.load_mesh(self.mesh_rawdata, relations=["CV", "VV", "CE", "EV"])
            self.mesh.verts.place({
                'x': ti.math.vec3,
                'v': ti.math.vec3,
                'f': ti.math.vec3
            })
            self.mesh.verts.x.from_numpy(self.mesh.get_position_as_numpy())
            self.mesh.verts.v.fill(0.0)
            self.mesh.verts.f.fill(0.0)
            self.indices = ti.field(ti.u32, shape=len(self.mesh.cells) * 4 * 3)
            self.init_tet_indices()

        else:
            self.mesh_rawdata = mp.load_mesh_rawdata(filename)
            self.mesh = mp.load_mesh(self.mesh_rawdata, relations=["FV"])
            self.mesh.verts.place({'x': ti.math.vec3})
            self.mesh.verts.x.from_numpy(self.mesh.get_position_as_numpy())
            self.indices = ti.field(ti.i32, shape=len(self.mesh.faces) * 3)
            self.init_surf_indices()

        self.vert_num = len(self.mesh.verts)
        self.center = ti.Vector.field(3, ti.f32, shape=())
        self.I = ti.Matrix([[1, 0, 0], [0, 1, 0], [0, 0, 1]], ti.i32)
        self.cal_barycenter()

    @ti.kernel
    def init_surf_indices(self):
        for f in self.mesh.faces:
            for j in ti.static(range(3)):
                self.indices[f.id * 3 + j] = f.verts[j].id

    @ti.kernel
    def init_tet_indices(self):
        for c in self.mesh.cells:
            ind = [[0, 2, 1], [0, 3, 2], [0, 1, 3], [1, 2, 3]]
            for i in ti.static(range(4)):
                for j in ti.static(range(3)):
                    self.indices[(c.id * 4 + i) * 3 + j] = c.verts[ind[i][j]].id

    @ti.kernel
    def cal_barycenter(self):
        for i in self.mesh.verts.x:
            self.center[None] += self.mesh.verts.x[i]
        self.center[None] /= self.vert_num


@ti.data_oriented
class Explicit(LoadModel):  # This class only for tetrahedron
    def __init__(self, filename, v_norm=1):
        super().__init__(filename)
        self.v_norm = v_norm

        self.dt = 7e-4
        self.gravity = ti.Vector([0.0, -9.8, 0.0])
        self.e = 2e6  # 杨氏模量
        self.nu = 0.1  # 泊松系数
        self.mu = self.e / (2 * (1 + self.nu))
        self.la = self.e * self.nu / ((1 + self.nu) * (1 - 2 * self.nu))
        self.density = 1e5

        self.cell_num = len(self.mesh.cells)
        self.V = ti.field(dtype=ti.f32, shape=())
        self.Dm = ti.Matrix.field(3, 3, ti.f32, shape=self.cell_num)  # Dm
        self.W = ti.field(ti.f32, shape=self.cell_num)  # 四面体体积
        self.B = ti.Matrix.field(3, 3, ti.f32, shape=self.cell_num)  # Dm逆
        self.m = ti.field(ti.f32, shape=self.vert_num)  # 点的质量
        self.F = ti.Matrix.field(3, 3, ti.f32, shape=self.cell_num)
        self.E = ti.Matrix.field(3, 3, ti.f32, shape=self.cell_num)

        self.norm_volume()
        self.fem_pre_cal()

    @ti.kernel
    def norm_volume(self):
        for cell in self.mesh.cells:
            v = ti.Matrix.zero(ti.f32, 3, 3)
            for i in ti.static(range(3)):
                for j in ti.static(range(3)):
                    v[j, i] = self.mesh.verts.x[cell.verts[i].id][j] - self.mesh.verts.x[cell.verts[3].id][j]
            self.V[None] += -(1.0 / 6.0) * v.determinant()
        if self.v_norm == 1:
            for vert in self.mesh.verts:
                vert.x *= 1000 / self.V[None]

    @ti.kernel
    def fem_pre_cal(self):  # fem参数预计算
        self.V[None] = 0
        for cell in self.mesh.cells:
            for i in ti.static(range(3)):
                for j in ti.static(range(3)):
                    self.Dm[cell.id][j, i] \
                        = self.mesh.verts.x[cell.verts[i].id][j] - self.mesh.verts.x[cell.verts[3].id][j]
            self.B[cell.id] = self.Dm[cell.id].inverse()
            self.W[cell.id] = -(1.0 / 6.0) * self.Dm[cell.id].determinant()
            self.V[None] += self.W[cell.id]
            for i in ti.static(range(4)):
                self.m[cell.verts[i].id] += 0.25 * self.density * self.W[cell.id]  # 把体元质量均分到四个顶点

    @ti.kernel
    def fem_get_force(self):  # 实时力计算
        for vert in self.mesh.verts:
            vert.f = self.gravity * self.m[vert.id]
        for cell in self.mesh.cells:
            Ds = ti.Matrix.zero(ti.f32, 3, 3)
            for i in ti.static(range(3)):
                for j in ti.static(range(3)):
                    Ds[j, i] \
                        = self.mesh.verts.x[cell.verts[i].id][j] - self.mesh.verts.x[cell.verts[3].id][j]
            self.F[cell.id] = Ds @ self.B[cell.id]
            self.E[cell.id] = 0.5 * (self.F[cell.id].transpose() @ self.F[cell.id] - self.I)
            U, sig, V = self.ssvd(self.F[cell.id])
            P = 2 * self.mu * (self.F[cell.id] - U @ V.transpose())
            # P = self.F[cell.id] @ (2 * self.mu * self.E[cell.id] + self.la * self.E[cell.id].trace() * self.I)
            H = -self.W[cell.id] * P @ self.B[cell.id].transpose()
            for i in ti.static(range(3)):
                fi = ti.Vector([H[0, i], H[1, i], H[2, i]])
                self.mesh.verts.f[cell.verts[i].id] += fi
                self.mesh.verts.f[cell.verts[3].id] += -fi

    @ti.func
    def ssvd(self, fai):
        U, sig, V = ti.svd(fai)
        if U.determinant() < 0:
            for i in ti.static(range(3)):
                U[i, 2] *= -1
            sig[2, 2] = -sig[2, 2]
        if V.determinant() < 0:
            for i in ti.static(range(3)):
                V[i, 2] *= -1
            sig[2, 2] = -sig[2, 2]
        return U, sig, V

    @ti.kernel
    def explicit_time_integral(self):
        for vert in self.mesh.verts:
            vert.v += self.dt * vert.f / self.m[vert.id] * 0.0000125
            vert.x += vert.v * self.dt

    @ti.kernel
    def boundary_condition(self):
        bounds = ti.Vector([1.0, 0.1, 1.0])
        for vert in self.mesh.verts:
            for i in ti.static(range(3)):
                if vert.x[i] < -bounds[i]:
                    vert.x[i] = -bounds[i]
                    if vert.v[i] < 0.0:
                        vert.v[i] = 0.0
                if vert.x[i] > bounds[i]:
                    vert.x[i] = bounds[i]
                    if vert.v[i] > 0.0:
                        vert.v[i] = 0.0

    def substep(self, step):
        for i in range(step):
            self.fem_get_force()
            self.explicit_time_integral()
            self.boundary_condition()
@ti.data_oriented
class Implicit(LoadModel):
    def __init__(self, filename, v_norm=1):
        super().__init__(filename)
        self.v_norm = v_norm

        self.dt = 1.0 / 30
        self.gravity = ti.Vector([0.0, -9.8, 0.0])
        self.e = 1e5  # 杨氏模量
        self.nu = 0.1  # 泊松系数
        self.mu = self.e / (2 * (1 + self.nu))
        self.la = self.e * self.nu / ((1 + self.nu) * (1 - 2 * self.nu))
        self.density = 1e5

        self.cell_num = len(self.mesh.cells)
        self.V = ti.field(dtype=ti.f32, shape=())
        self.Dm = ti.Matrix.field(3, 3, ti.f32, shape=self.cell_num)  # Dm
        self.W = ti.field(ti.f32, shape=self.cell_num)  # 四面体体积
        self.B = ti.Matrix.field(3, 3, ti.f32, shape=self.cell_num)  # Dm逆
        self.m = ti.field(ti.f32, shape=self.vert_num)  # 点的质量
        self.F = ti.Matrix.field(3, 3, ti.f32, shape=self.cell_num)
        self.E = ti.Matrix.field(3, 3, ti.f32, shape=self.cell_num)

        self.b = ti.Vector.field(3, dtype=ti.f32, shape=self.vert_num)
        self.r0 = ti.Vector.field(3, dtype=ti.f32, shape=self.vert_num)
        self.p0 = ti.Vector.field(3, dtype=ti.f32, shape=self.vert_num)
        self.dot_ans = ti.field(ti.f32, shape=())
        self.r_2_scalar = ti.field(ti.f32, shape=())

        self.mul_ans = ti.Vector.field(3, dtype=ti.f32, shape=self.vert_num)
        self.norm_volume()
        self.fem_pre_cal()

    @ti.kernel
    def norm_volume(self):
        for cell in self.mesh.cells:
            v = ti.Matrix.zero(ti.f32, 3, 3)
            for i in ti.static(range(3)):
                for j in ti.static(range(3)):
                    v[j, i] = self.mesh.verts.x[cell.verts[i].id][j] - self.mesh.verts.x[cell.verts[3].id][j]
            self.V[None] += -(1.0 / 6.0) * v.determinant()
        if self.v_norm == 1:
            for vert in self.mesh.verts:
                vert.x *= 1000 / self.V[None]

    @ti.kernel
    def fem_pre_cal(self):
        for cell in self.mesh.cells:
            for i in ti.static(range(3)):
                for j in ti.static(range(3)):
                    self.Dm[cell.id][j, i] = \
                        self.mesh.verts.x[cell.verts[i].id][j] - self.mesh.verts.x[cell.verts[3].id][j]
            self.B[cell.id] = self.Dm[cell.id].inverse()  # Dm逆
            self.W[cell.id] = -(1.0 / 6.0) * self.Dm[cell.id].determinant()  # 四面体体积
            for i in ti.static(range(4)):
                self.m[cell.verts[i].id] += 0.25 * self.density * self.W[cell.id]  # 把体元质量均分到四个顶点

    @ti.kernel
    def fem_get_force(self):  # 实时力计算
        for vert in self.mesh.verts:
            vert.f = self.gravity * self.m[vert.id]
        for cell in self.mesh.cells:
            Ds = ti.Matrix.zero(ti.f32, 3, 3)
            for i in ti.static(range(3)):
                for j in ti.static(range(3)):
                    Ds[j, i] \
                        = self.mesh.verts.x[cell.verts[i].id][j] - self.mesh.verts.x[cell.verts[3].id][j]
            self.F[cell.id] = Ds @ self.B[cell.id]
            self.E[cell.id] = 0.5 * (self.F[cell.id].transpose() @ self.F[cell.id] - self.I)
            U, sig, V = self.ssvd(self.F[cell.id])
            P = 2 * self.mu * (self.F[cell.id] - U @ V.transpose())
            # P = self.F[cell.id] @ (2 * self.mu * self.E[cell.id] + self.la * self.E[cell.id].trace() * self.I)
            H = -self.W[cell.id] * P @ self.B[cell.id].transpose()
            for i in ti.static(range(3)):
                fi = ti.Vector([H[0, i], H[1, i], H[2, i]])
                self.mesh.verts.f[cell.verts[i].id] += fi
                self.mesh.verts.f[cell.verts[3].id] += -fi

    @ti.kernel
    def fem_get_b(self):  # 取初始值x=xn,计算一阶梯度
        for vert in self.mesh.verts:
            self.b[vert.id] = self.m[vert.id] * vert.v + self.dt * vert.f

    @ti.kernel
    def mat_mul(self, ret: ti.template(), vel: ti.template()):
        for vert in self.mesh.verts:
            ret[vert.id] = vel[vert.id] * self.m[vert.id]
        for cell in self.mesh.cells:
            verts = cell.verts
            W_c = self.W[cell.id]
            B_c = self.B[cell.id]
            for u in ti.static(range(4)):
                for d in (range(3)):
                    dD = ti.Matrix.zero(ti.f32, 3, 3)
                    if u == 3:
                        for j in ti.static(range(3)):
                            dD[d, j] = -1
                    else:
                        dD[d, u] = 1
                    dF = dD @ B_c
                    dP = 2.0 * self.mu * dF
                    dH = -W_c * dP @ B_c.transpose()
                    for i in ti.static(range(3)):
                        for j in ti.static(range(3)):
                            tmp = (vel[verts[i].id][j] - vel[verts[3].id][j])
                            ret[verts[u].id][d] += -self.dt ** 2 * dH[j, i] * tmp

    @ti.kernel
    def add(self, ans: ti.template(), a: ti.template(), k: ti.f32, x3: ti.template()):
        for i in ans:
            ans[i] = a[i] + k * x3[i]

    @ti.kernel
    def dot(self, x1: ti.template(), x2: ti.template()) -> ti.f32:
        ans = 0.0
        for i in x1:
            ans += x1[i].dot(x2[i])
        return ans

    def cg(self, n_iter, epsilon):
        self.mat_mul(self.mul_ans, self.mesh.verts.v)
        self.add(self.r0, self.b, -1, self.mul_ans)
        self.p0.copy_from(self.r0)
        r_2 = self.dot(self.r0, self.r0)
        r_2_init = r_2
        r_2_new = r_2
        for _ in ti.static(range(n_iter)):
            self.mat_mul(self.mul_ans, self.p0)
            dot_ans = self.dot(self.p0, self.mul_ans)
            alpha = r_2_new / (dot_ans + epsilon)
            self.add(self.mesh.verts.v, self.mesh.verts.v, alpha, self.p0)
            self.add(self.r0, self.r0, -alpha, self.mul_ans)
            r_2 = r_2_new
            r_2_new = self.dot(self.r0, self.r0)
            if r_2_new <= r_2_init * epsilon ** 2:
                break
            beta = r_2_new / r_2
            self.add(self.p0, self.r0, beta, self.p0)
        self.add(self.mesh.verts.x, self.mesh.verts.x, self.dt, self.mesh.verts.v)

    @ti.kernel
    def boundary_condition(self):
        bounds = ti.Vector([1.0, 0.1, 1.0])
        for vert in self.mesh.verts:
            for i in ti.static(range(3)):
                if vert.x[i] < -bounds[i]:
                    vert.x[i] = -bounds[i]
                    if vert.v[i] < 0.0:
                        vert.v[i] = 0.0
                if vert.x[i] > bounds[i]:
                    vert.x[i] = bounds[i]
                    if vert.v[i] > 0.0:
                        vert.v[i] = 0.0

    def substep(self, step):
        for i in range(step):
            self.fem_get_force()
            self.fem_get_b()
            self.cg(10, 1e-6)
            self.boundary_condition()

    @ti.func
    def ssvd(self, fai):
        U, sig, V = ti.svd(fai)
        if U.determinant() < 0:
            for i in ti.static(range(3)):
                U[i, 2] *= -1
            sig[2, 2] = -sig[2, 2]
        if V.determinant() < 0:
            for i in ti.static(range(3)):
                V[i, 2] *= -1
            sig[2, 2] = -sig[2, 2]
        return U, sig, V
